"""Speech-to-text (M2). faster-whisper + VAD; transcribed request -> text."""

from __future__ import annotations


class Transcriber:
    def __init__(self, model: str = "auto", language: str = "auto"):
        self.model = model
        self.language = language

    def transcribe(self, audio_bytes: bytes) -> str:
        raise NotImplementedError("M2")