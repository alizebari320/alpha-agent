"""Request recording: capture speech after the wake word until silence (§1.3).

Uses the Silero VAD (bundled with openwakeword) as the primary gate, with
webrtc-style energy fallback if the VAD model can't load.
"""

from __future__ import annotations

import logging
import time
from collections import deque

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

    def speech_gate(self, pcm: np.ndarray) -> tuple[float, float]:
        """(speech_ratio, rms) in ONE call — both halves of the wake gate.

        The wake path calls this every couple of seconds forever, and each
        transcribe it lets through costs a fixed ~0.65 s of CPU (whisper pads
        every input to a 30 s mel chunk, so the price does not shrink with the
        window). Doing both checks in one hop keeps that path to a single
        thread hand-off, and the RMS floor rejects continuous room tone that the
        VAD alone will happily call speech.
        """
        return self.speech_ratio(pcm), rms(pcm)


class NoiseFloor:
    """Rolling estimate of room tone, so the wake gate adapts to the room.

    A fixed `min_rms` cannot be right everywhere. Measured on the development
    machine: room tone sat at rms 280-955 while speech was ~2000, so a default of
    120 let noise through; and silero's VAD on its own called fan/desk noise
    speech often enough that the wake path paid a whisper decode (~0.65 s of CPU)
    every ~12 s during one quiet stretch, with whisper answering in its classic
    hallucination set ("Thank you.", "I'm sorry."). See docs/PERFORMANCE.md.

    So: remember the quiet end of the last ~1 minute of windows (the 10th
    percentile) and require a window to stand a factor above it *and* above the
    configured absolute floor. Intermittent speech does not move a low
    percentile, and the absolute floor keeps a genuinely silent room from
    normalising its way down to noise.
    """

    def __init__(self, floor_min: float = 300.0, history: int = 40,
                 factor: float = 2.0):
        self.floor_min = float(floor_min)
        self.factor = float(factor)
        self._levels: deque[float] = deque(maxlen=history)

    def observe(self, level: float) -> None:
        self._levels.append(float(level))

    @property
    def room_tone(self) -> float:
        if not self._levels:
            return 0.0
        ordered = sorted(self._levels)
        idx = max(0, int(len(ordered) * 0.10) - 1)
        return ordered[idx]

    def threshold(self) -> float:
        """Level a window must exceed to be worth a decode."""
        return max(self.floor_min, self.factor * self.room_tone)


# A window must clear BOTH thresholds before Alpha spends a whisper decode on
# it (spec §8 Tier 3). Kept here, next to the measurement, so the daemon and
# `alpha doctor --levels` cannot disagree about what the gate does.
KWS_MIN_SPEECH_RATIO = 0.15


def wake_gate_opens(ratio: float, level: float, min_rms: float,
                    min_ratio: float = KWS_MIN_SPEECH_RATIO) -> bool:
    """Should this window be transcribed? (loud enough AND speech-like enough)"""
    return ratio >= min_ratio and level >= min_rms


def rms(pcm: np.ndarray) -> float:
    """Root-mean-square of an int16 buffer, in int16 units (0..32768)."""
    if len(pcm) == 0:
        return 0.0
    a = pcm.astype(np.float32)
    return float(np.sqrt(np.mean(a * a)))
