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

from . import models

log = logging.getLogger(__name__)

# What openwakeword actually ships. (Verified against openwakeword.MODELS.)
BUNDLED_WAKEWORDS = ("hey_jarvis", "hey_mycroft", "hey_rhasspy", "alexa")

FRAME = 1280  # 80 ms @ 16kHz, openwakeword's expected stride


class PretrainedWake:
    """openWakeWord ONNX model, frame by frame."""

    def __init__(self, model_name: str, threshold: float = 0.5, refractory_s: float = 2.0):
        if model_name not in BUNDLED_WAKEWORDS:
            raise ValueError(
                f"'{model_name}' has no shipped pretrained model; bundled: {BUNDLED_WAKEWORDS}. "
                f"Use mode='kws' for custom names like 'hey alpha'."
            )
        from openwakeword.model import Model

        models.ensure_openwakeword([model_name])
        self.model_name = model_name
        self.threshold = threshold
        self.refractory_s = refractory_s
        self._last_fire = 0.0
        self._oww = Model(
            wakeword_models=[model_name],
            inference_framework="onnx",
            vad_threshold=0.0,
        )
        log.info("wake word (pretrained): %s", model_name)

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

    def __init__(self, phrase: str, transcriber=None, threshold: float = 0.6):
        self.phrase = self._normalize(phrase)
        # Always whisper-tiny for KWS, regardless of the STT request model.
        if transcriber is None:
            from .stt import Transcriber

            transcriber = Transcriber(model="tiny", language="en")
        self.transcriber = transcriber
        self.threshold = threshold
        log.info("wake word (kws): %r (whisper-tiny)", phrase)

    @staticmethod
    def _normalize(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"[^\w\s]", "", s)
        # phonetic normalization: english pronounces 'ph' as /f/, so STT of a
        # spoken "alpha" is routinely "alfa". Normalizing both sides makes
        # that an exact match.
        return s.replace("ph", "f")

    def feed_utterance(self, pcm: np.ndarray) -> bool:
        """Transcribe a short VAD-gated utterance; fuzzy-match the phrase.

        Matching is deliberately forgiving: STT of a spoken custom name is
        noisy ('hey alpha' -> 'hey alfa', 'a alpha', 'hi alpha'...)."""
        text = self._normalize(self.transcriber.transcribe(pcm))
        if not text:
            return False
        # 1) exact containment
        if self.phrase in text:
            log.info("wake! kws exact match %r in %r", self.phrase, text)
            return True
        words = text.split()
        phrase_words = self.phrase.split()
        # 2) sliding-window fuzzy over word n-grams
        for n in range(max(1, len(phrase_words) - 1), len(phrase_words) + 2):
            for i in range(max(1, len(words) - n + 1)):
                window = " ".join(words[i: i + n])
                ratio = difflib.SequenceMatcher(None, self.phrase, window).ratio()
                if ratio >= self.threshold:
                    log.info("wake! kws fuzzy %r ~ %r (%.2f)", self.phrase, window, ratio)
                    return True
        # 3) per-word fuzzy: every phrase word matches SOME transcript word
        #    (order-tolerant, handles 'hey alfa' / 'a lpha' / 'hi alpha')
        if self._all_words_present(phrase_words, words):
            log.info("wake! kws per-word match %r in %r", self.phrase, text)
            return True
        return False

    def _all_words_present(self, phrase_words: list[str], words: list[str],
                           per_word_threshold: float = 0.65) -> bool:
        for pw in phrase_words:
            best = max((difflib.SequenceMatcher(None, pw, w).ratio() for w in words),
                       default=0.0)
            if best < per_word_threshold:
                return False
        return True
