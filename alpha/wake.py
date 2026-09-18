"""Wake word detection (Tier 1 + Tier 3 from §8).

- Tier 1 ("pretrained"): openWakeWord ONNX models, ~realtime on CPU. Bundled
  names: hey_jarvis, hey_mycroft, hey_rhasspy, alexa (openwakeword's set —
  there is NO pretrained "hey_alpha", so Alpha's own name must use Tier 3).
- Tier 3 ("kws"): keyword spotting with faster-whisper on rolling VAD-gated
  utterances, fuzzy-matching the configured phrase. Works with ANY name
  instantly, but uses more CPU (only while speech is actually happening).

Chosen via config assistant.wake_word.mode.
"""

from __future__ import annotations

import difflib
import logging
import re
import time

import numpy as np

from . import models, paths

log = logging.getLogger(__name__)

# What openwakeword actually ships. (Verified against openwakeword.MODELS.)
BUNDLED_WAKEWORDS = ("hey_jarvis", "hey_mycroft", "hey_rhasspy", "alexa")

FRAME = 1280  # 80 ms @ 16kHz, openwakeword's expected stride


def resolve_pretrained_model(name: str) -> str:
    """Map a wake-word name to something openWakeWord's ``Model()`` accepts.

    openWakeWord takes either a bundled model name or a path to an ONNX/tflite
    file. Alpha's config accepts a custom name when
    ``~/.local/share/alpha/wakewords/<name>.onnx`` exists (that is where
    ``scripts/train-wake-word.py --install`` puts it), but this function used to
    reject anything outside ``BUNDLED_WAKEWORDS`` — so a trained model passed
    config validation and then crashed the daemon here with "no shipped
    pretrained model". The whole train → install → use flow was broken.

    Returns the bundled name unchanged, or the path to the trained model.
    """
    if name in BUNDLED_WAKEWORDS:
        return name
    for suffix in (".onnx", ".tflite"):
        custom = paths.WAKEWORD_DIR / f"{name}{suffix}"
        if custom.is_file():
            return str(custom)
    raise ValueError(
        f"no wake word model named '{name}': not one of the bundled models "
        f"{list(BUNDLED_WAKEWORDS)}, and no trained model at "
        f"{paths.WAKEWORD_DIR / (name + '.onnx')}. Train one with "
        f"scripts/train-wake-word.py, or use mode='kws'."
    )


class PretrainedWake:
    """openWakeWord ONNX model, frame by frame."""

    def __init__(self, model_name: str, threshold: float = 0.5, refractory_s: float = 2.0):
        from openwakeword.model import Model

        resolved = resolve_pretrained_model(model_name)  # raises if it is unknown
        bundled = model_name in BUNDLED_WAKEWORDS
        if bundled:
            models.ensure_openwakeword([model_name])
        # Match the framework to the file we actually resolved to; asking for
        # onnx while handing over a .tflite path fails at load time.
        framework = "tflite" if resolved.endswith(".tflite") else "onnx"
        self.model_name = model_name
        self.threshold = threshold
        self.refractory_s = refractory_s
        self._last_fire = 0.0
        self._oww = Model(
            wakeword_models=[resolved],
            inference_framework=framework,
            vad_threshold=0.0,
        )
        log.info("wake word (pretrained): %s%s", model_name,
                 "" if bundled else f" [trained: {resolved}]")

    def feed(self, frame: np.ndarray) -> bool:
        """Frame: int16 np array (len==1280). Returns True when woken."""
        preds = self._oww.predict(frame)
        score = max(preds.values())
        now = time.time()
        if score >= self.threshold and (now - self._last_fire) >= self.refractory_s:
            self._last_fire = now
            log.info("wake! score=%.3f (%s)", score, self.model_name)
            return True
        return False

    def reset(self) -> None:
        try:
            self._oww.reset()
        except AttributeError:
            pass


class KWSWake:
    """Keyword spotting: whisper-tiny on rolling 2 s speech segments.

    Uses its OWN tiny model (not the request transcriber) so the §9 idle
    budget holds: only whisper-tiny is resident while waiting for the wake word.
    """

    def __init__(self, phrase: str | list[str], transcriber=None, threshold: float = 0.6):
        if isinstance(phrase, str):
            phrase = [phrase]
        self.phrases = [self._normalize(x) for x in phrase if x and x.strip()]
        if not self.phrases:
            self.phrases = ["hey alpha"]
        self.phrase = self.phrases[0]
        # Always whisper-tiny for KWS, regardless of the STT request model.
        if transcriber is None:
            from .stt import Transcriber

            # cpu_threads=1: this decoder runs in the background forever.
            transcriber = Transcriber(model="tiny", language="en", cpu_threads=1)
        self.transcriber = transcriber
        self.threshold = threshold
        log.info("wake word (kws): %s (whisper-tiny)", " | ".join(repr(x) for x in self.phrases))

    @staticmethod
    def _normalize(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"[^\w\s]", "", s)
        # phonetic normalization: english pronounces 'ph' as /f/, so STT of a
        # spoken "alpha" is routinely "alfa". Normalizing both sides makes
        # that an exact match.
        return s.replace("ph", "f")

    def feed_utterance(self, pcm: np.ndarray) -> bool:
        """Transcribe a short VAD-gated utterance; fuzzy-match a wake phrase.

        Matching is deliberately forgiving because whisper-tiny mangles an
        arbitrary name ('hey alpha' -> 'hey alfa', 'hi alpha' -> 'high alpha').
        But it must NOT be so forgiving that ordinary speech trips it, so:
          * the WHOLE phrase must be present, in order — a window equal to the
            phrase length is the only short window we accept (''hey alpha' ~
            'half' (0.67)' woke Alpha on random TV audio and is the reason for
            this rule);
          * extra words are only tolerated AFTER the phrase (the phrase is what
            the user is saying to Alpha);
          * per-word fallback requires every phrase word to find a good match,
            and phrase words under 4 chars must match almost exactly.
        """
        text = self._normalize(self.transcriber.transcribe(pcm))
        if not text:
            return False
        words = text.split()
        for phrase in self.phrases:
            hit = self._match_one(phrase, words, text)
            if hit:
                return True
        return False

    def _match_one(self, phrase: str, words: list[str], text: str) -> bool:
        if phrase in text:
            log.info("wake! kws exact match %r in %r", phrase, text)
            return True
        pw = phrase.split()
        # 2) sliding window, restricted to phrase-length windows at position 0
        #    and any window that starts at the beginning of the transcript.
        n = len(pw)
        for i in range(max(1, len(words) - n + 1)):
            window = " ".join(words[i: i + n])
            ratio = difflib.SequenceMatcher(None, phrase, window).ratio()
            if ratio >= self.threshold and (i == 0 or (i + n == len(words))):
                log.info("wake! kws fuzzy %r ~ %r (%.2f)", phrase, window, ratio)
                return True
        # 3) prefix match: the phrase, then extra words ('hey alpha, what time
        #    is it' said in one breath). Compare against the first len(pw) words
        #    and against progressively longer prefixes.
        for extra in range(0, max(3, len(words))):
            window = " ".join(words[: n + extra])
            ratio = difflib.SequenceMatcher(None, phrase, window).ratio()
            if ratio >= self.threshold:
                log.info("wake! kws fuzzy %r ~ prefix %r (%.2f)", phrase, window, ratio)
                return True
        # 4) per-word fuzzy: every phrase word matches SOME transcript word
        if self._all_words_present(pw, words):
            log.info("wake! kws per-word match %r in %r", phrase, text)
            return True
        # 5) greeting-variant match: whisper-tiny turns a spoken greeting into
        #    almost anything ('hey alpha' -> "Hey y'all for", 'hi alpha' ->
        #    "Ha, y'all for"). For a '<greeting> <name>' phrase, accept any
        #    common greeting opening as long as the NAME matches clearly — this
        #    is what makes "hi alpha" / "hello alpha" wake Alpha, while a bare
        #    "alpha" or "hi siri" still does not.
        if self._greeting_and_name(pw, words):
            log.info("wake! kws greeting-variant match %r in %r", phrase, text)
            return True
        return False

    GREETINGS = ("hey", "hi", "hello", "okay", "ok", "yo", "ahoy", "hej", "ha")

    def _greeting_and_name(self, phrase_words: list[str], words: list[str],
                           name_threshold: float = 0.75) -> bool:
        if len(phrase_words) != 2:
            return False
        greeting, name = phrase_words
        if greeting not in self.GREETINGS:
            return False
        said_greeting = any(w in self.GREETINGS or
                            difflib.SequenceMatcher(None, greeting, w).ratio() >= 0.8
                            for w in words)
        if not said_greeting:
            return False
        best_name = max((difflib.SequenceMatcher(None, name, w).ratio() for w in words),
                        default=0.0)
        return best_name >= name_threshold

    def _all_words_present(self, phrase_words: list[str], words: list[str],
                           per_word_threshold: float = 0.65) -> bool:
        """Every phrase word must match some transcript word.

        Short words ('hey', 'hi') carry little information, so they get a much
        stricter bar: at 0.65, 'hi' matched 'i' and any stray vowel, which is
        how background speech used to wake Alpha.
        """
        for pw in phrase_words:
            need = 0.9 if len(pw) <= 3 else per_word_threshold
            best = max((difflib.SequenceMatcher(None, pw, w).ratio() for w in words),
                       default=0.0)
            if best < need:
                return False
        return True
