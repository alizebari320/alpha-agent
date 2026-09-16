"""Audio I/O for Alpha: microphone capture, playback, ack sounds.

Everything runs over PipeWire via sounddevice (PortAudio), 16 kHz mono int16
internally. Mic capture is always bounded — the ring buffer holds a fixed
amount of audio (§9: never accumulate unbounded audio).
"""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SAMPLERATE = 16000
FRAME_MS = 80
FRAME_SAMPLES = SAMPLERATE * FRAME_MS // 1000  # 1280 samples


class RingBuffer:
    """Thread-safe fixed-size int16 sample ring."""

    def __init__(self, capacity_s: float = 16.0, samplerate: int = SAMPLERATE):
        self.capacity = int(capacity_s * samplerate)
        self._buf = np.zeros(self.capacity, dtype=np.int16)
        self._n = 0  # valid samples
        self._lock = threading.Lock()

    def append(self, chunk: np.ndarray) -> None:
        chunk = np.asarray(chunk, dtype=np.int16).ravel()
        with self._lock:
            combined = np.concatenate([self._buf[: self._n], chunk])
            if len(combined) > self.capacity:
                combined = combined[-self.capacity:]
            self._buf[: len(combined)] = combined
            self._n = len(combined)

    def snapshot(self) -> np.ndarray:
        with self._lock:
            return self._buf[: self._n].copy()

    def pop(self, n: int) -> np.ndarray:
        """Consume and return up to n samples from the head (empty if none)."""
        with self._lock:
            take = min(n, self._n)
            if take <= 0:
                return np.zeros(0, dtype=np.int16)
            out = self._buf[:take].copy()
            self._buf[: self._n - take] = self._buf[take: self._n]
            self._n -= take
            return out

    def available(self) -> int:
        return self._n

    def clear(self) -> None:
        with self._lock:
            self._n = 0

    def __len__(self) -> int:
        return self._n


class Mic:
    """Rolling mic capture. Start once; audio accumulates in `ring`."""

    def __init__(self, device: int | None = None, ring_s: float = 16.0):
        self.ring = RingBuffer(capacity_s=ring_s)
        self.device = device
        self._stream = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        import sounddevice as sd

        def _cb(indata, frames, time_info, status):
            if status:
                log.debug("mic status: %s", status)
            mono = indata[:, 0] if indata.ndim > 1 else indata
            # float32 [-1,1] -> int16
            self.ring.append((np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16))

        self._stream = sd.InputStream(
            samplerate=SAMPLERATE,
            channels=1,
            dtype="float32",
            blocksize=FRAME_SAMPLES,
            device=self.device,
            callback=_cb,
        )
        self._stream.start()
        self._running = True
        log.info("microphone open (device=%s)", self.device or "default")

    def stop(self, release_device: bool = True) -> None:
        """Stop capture. With release_device=True the h/w mic is genuinely
        released (used by MUTE — §10), so nothing keeps the mic lit."""
        if not self._running:
            return
        assert self._stream is not None
        self._stream.stop()
        if release_device:
            self._stream.close()
            self._stream = None
        self._running = False
        log.info("microphone %s", "released" if release_device else "paused")

    def read_frames(self, n_frames: int) -> np.ndarray:
        """Blocking-ish helper: wait until ring has n_frames and pop them as
        int16 (used by the wake loop which consumes frames as they arrive)."""
        return self.ring.snapshot()

    def pop_samples(self, n: int) -> np.ndarray | None:
        """Consume and return exactly n samples, or None if not enough yet."""
        out = self.ring.pop(n)
        return out if len(out) == n else None

    def clear(self) -> None:
        self.ring.clear()


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


class Speaker:
    """Plays int16 PCM to the default output, interruptibly (barge-in, §11)."""

    def __init__(self, device: int | None = None):
        self.device = device
        self._current = None  # sounddevice stream-like
        self._lock = threading.Lock()

    def play_pcm(self, pcm: np.ndarray, samplerate: int) -> None:
        import sounddevice as sd

        audio = (pcm.astype(np.float32) / 32767.0)
        self.stop()  # duck any current speech
        with self._lock:
            sd.play(audio, samplerate=samplerate, device=self.device, blocking=False)

    def stop(self) -> None:
        import sounddevice as sd

        sd.stop()

    def wait_done(self) -> None:
        import sounddevice as sd

        sd.wait()


# ---------------------------------------------------------------------------
# Ack / state sounds (synthesised sine beeps — no assets needed)
# ---------------------------------------------------------------------------


def _tone(freq: float, dur_s: float, samplerate: int = 22050, level: float = 0.35) -> np.ndarray:
    t = np.linspace(0, dur_s, int(dur_s * samplerate), endpoint=False)
    env = np.minimum(1.0, np.minimum(t / 0.01, (dur_s - t) / 0.03))  # soft edges
    pcm = level * env * np.sin(2 * np.pi * freq * t)
    return (np.clip(pcm, -1, 1) * 32767).astype(np.int16)


def ack_sound() -> tuple[np.ndarray, int]:
    """'Wake acknowledged' — pleasant rising double blip."""
    sr = 22050
    return np.concatenate([_tone(880, 0.09, sr), _tone(1320, 0.12, sr)]), sr


def error_sound() -> tuple[np.ndarray, int]:
    sr = 22050
    return np.concatenate([_tone(440, 0.15, sr), _tone(330, 0.2, sr)]), sr


def done_sound() -> tuple[np.ndarray, int]:
    sr = 22050
    return np.concatenate([_tone(660, 0.08, sr), _tone(990, 0.08, sr), _tone(1320, 0.12, sr)]), sr


# ---------------------------------------------------------------------------
# WAV helpers (for writing request audio to disk when debugging)
# ---------------------------------------------------------------------------


def write_wav(path: Path, pcm: np.ndarray, samplerate: int = SAMPLERATE) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(samplerate)
        w.writeframes(pcm.astype(np.int16).tobytes())
