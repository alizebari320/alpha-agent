"""Text-to-speech (M2). piper (English + Arabic voices)."""

from __future__ import annotations


class Speaker:
    def __init__(self, voice: str):
        self.voice = voice

    def speak(self, text: str, language: str = "en") -> None:
        raise NotImplementedError("M2")