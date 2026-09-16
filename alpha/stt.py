"""Speech-to-text via faster-whisper (CTranslate2, int8, CPU).

Lazy-loaded (§9): nothing is resident until the first request. The same
instance serves the KWS wake matcher and request transcription.
"""

from __future__ import annotations

import logging

import numpy as np

from . import models

log = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, model: str = "auto", language: str = "auto",
                 cpu_threads: int = 0):
        self.model_name = models.pick_whisper_model(model)
        self.language = language
        self.cpu_threads = cpu_threads
        self._model = None  # lazy

    def _load(self):
        if self._model is None:
            self._model = models.ensure_whisper(self.model_name, self.cpu_threads)
        return self._model

    def preload(self) -> None:
        self._load()

    def unload(self) -> None:
        self._model = None
        log.info("whisper model unloaded")

    def transcribe(self, pcm: np.ndarray) -> str:
        pcm = np.asarray(pcm)
        if pcm.dtype == np.int16:
            audio = pcm.astype(np.float32) / 32768.0
        else:
            audio = pcm.astype(np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if len(audio) < 1600:  # < 100 ms — nothing to say
            return ""
        model = self._load()
        lang = None if self.language in ("auto", "", None) else self.language
        segments, _info = model.transcribe(audio, language=lang, beam_size=1, vad_filter=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        log.info("transcript: %r", text)
        return text
