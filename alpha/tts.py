"""Text-to-speech via piper (offline, fast). English + Arabic voices.

The voice .onnx downloads on first use via alpha.models.ensure_piper_voice.
Lazy-loaded and unloadable (§9).
"""

from __future__ import annotations

import io
import logging
import re
import wave

import numpy as np

from . import models
from .audio import Speaker

log = logging.getLogger(__name__)

_AR_RE = re.compile(r"[\u0600-\u06FF]")


def looks_arabic(text: str) -> bool:
    return bool(_AR_RE.search(text))


class SpeakerTTS:
    def __init__(self, voice: str, arabic_voice: str, volume: float = 1.0,
                 player: Speaker | None = None):
        self.voice_id = voice
        self.arabic_voice_id = arabic_voice
        self.volume = volume
        self.player = player or Speaker()
        self._voices: dict[str, object] = {}

    def _get_voice(self, voice_id: str):
        if voice_id not in self._voices:
            from piper import PiperVoice

            onnx, cfg = models.ensure_piper_voice(voice_id)
            self._voices[voice_id] = PiperVoice.load(str(onnx), config_path=str(cfg))
            log.info("piper voice loaded: %s", voice_id)
        return self._voices[voice_id]

    def preload(self) -> None:
        """Load the English voice now (Arabic loads on first Arabic reply)."""
        self._get_voice(self.voice_id)

    def unload(self) -> None:
        self._voices.clear()
        log.info("piper voices unloaded")

    def say(self, text: str) -> None:
        """Synthesize and play. Language auto-picked by content (§11)."""
        text = (text or "").strip()
        if not text:
            return
        voice_id = self.arabic_voice_id if looks_arabic(text) else self.voice_id
        voice = self._get_voice(voice_id)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            voice.synthesize_wav(text, w)
        buf.seek(0)
        with wave.open(buf, "rb") as w:
            sr = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if self.volume != 1.0:
            pcm = np.clip(pcm.astype(np.float32) * self.volume, -32768, 32767).astype(np.int16)
        log.info("speaking (%s): %r", voice_id, text[:80])
        self.player.play_pcm(pcm, sr)
        self.player.wait_done()

    def stop(self) -> None:
        self.player.stop()
