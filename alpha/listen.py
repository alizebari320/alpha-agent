"""Request recording: capture speech after the wake word until silence (§1.3).

Uses the Silero VAD (bundled with openwakeword) as the primary gate, with
webrtc-style energy fallback if the VAD model can't load.
"""

from __future__ import annotations

import logging
import time

import numpy as np

from .audio import FRAME_SAMPLES

log = logging.getLogger(__name__)


class Recorder:
    """Pull frames from a RingBuffer between a start point and endpoint."""

    def __init__(self, vad=None, max_utterance_s: float = 15.0,
                 silence_end_s: float = 1.4, min_voice_s: float = 0.25):
        self.vad = vad or self._load_vad()
        self.max_utterance_s = max_utterance_s
        self.silence_end_s = silence_end_s
        self.min_voice_s = min_voice_s

    @staticmethod
    def _load_vad():
        try:
            import openwakeword.utils  # noqa
            from openwakeword import VAD

            return VAD()
        except Exception as e:
            log.warning("silero VAD unavailable (%s); falling back to energy VAD", e)
            return None

    def is_speech(self, frame: np.ndarray) -> bool:
        if self.vad is not None:
            try:
                pred = self.vad.predict(frame, frame_size=len(frame))
                return float(np.max(pred)) >= 0.5
            except Exception:
                pass
        # energy fallback
        x = frame.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
        return rms > 0.01

    def record_utterance(self, ring, start_index: int | None = None) -> np.ndarray:
        """Collect frames from `ring` (a live alpha.audio.RingBuffer) until
        silence. The caller decides whether to clear pre-wake audio first."""
        voice_s = 0.0
        silence_s = 0.0
        collected = []
        deadline = time.time() + self.max_utterance_s
        log.info("recording request... (end on %.1fs silence, cap %.0fs)",
                 self.silence_end_s, self.max_utterance_s)

        frame_s = FRAME_SAMPLES / 16000.0
        while time.time() < deadline:
            frame = ring.pop(FRAME_SAMPLES)
            if len(frame) < FRAME_SAMPLES:
                time.sleep(0.01)
                continue
            speech = self.is_speech(frame)
            collected.append(frame)
            if speech:
                voice_s += frame_s
                silence_s = 0.0
            else:
                silence_s += frame_s
            if voice_s >= self.min_voice_s and silence_s >= self.silence_end_s:
                log.info("utterance end (voice=%.2fs)", voice_s)
                break

        if not collected:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(collected)

    def speech_ratio(self, pcm: np.ndarray) -> float:
        """Fraction of 80ms frames the VAD marks as speech."""
        n = len(pcm) // FRAME_SAMPLES
        if n == 0:
            return 0.0
        hits = sum(1 for i in range(n) if self.is_speech(pcm[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES]))
        return hits / n
