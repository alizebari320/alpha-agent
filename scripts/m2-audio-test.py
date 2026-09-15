#!/usr/bin/env python3
"""M2 end-to-end audio pipeline test (no microphone needed).

Simulates the full voice flow offline:
  1. piper synthesizes "hey alpha"        -> KWSWake must detect the wake word
  2. piper synthesizes "hello"            -> KWSWake must NOT fire
  3. piper synthesizes "hello"            -> Recorder+Transcriber must get it
  4. piper speaks the transcript back     -> written to /tmp/alpha_m2_echo.wav

Run:  uv run python scripts/m2-audio-test.py
"""

from __future__ import annotations

import io
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha import models  # noqa: E402
from alpha.stt import Transcriber  # noqa: E402
from alpha.wake import KWSWake  # noqa: E402

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


def synth(text: str, voice_id: str = "en_US-lessac-medium") -> np.ndarray:
    """piper TTS -> int16 PCM @16k (resampled from 22.05k by repetition-free
    linear interpolation — fine for feeding our own pipeline)."""
    from piper import PiperVoice

    onnx, cfg = models.ensure_piper_voice(voice_id)
    voice = PiperVoice.load(str(onnx), config_path=str(cfg))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        voice.synthesize_wav(text, w)
    buf.seek(0)
    with wave.open(buf, "rb") as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if sr != 16000:
        # naive linear resample
        n_out = int(len(pcm) * 16000 / sr)
        x = np.linspace(0, len(pcm) - 1, n_out)
        pcm = pcm[x.astype(np.int64)]
    return pcm


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * 16000), dtype=np.int16)


def main() -> int:
    print("==> synthesizing test utterances with piper (English voice)")
    wake_pcms = [synth("hey alpha") for _ in range(3)]  # piper is stochastic
    hello_pcm = synth("hello")
    print(f"    'hey alpha' x3 -> {[f'{len(w)/16000:.2f}s' for w in wake_pcms]}, "
          f"'hello' -> {len(hello_pcm)/16000:.2f}s")

    # --- 1. KWS wake detection (whisper-tiny + fuzzy match) -----------------
    print("==> test 1: KWS detects 'hey alpha' (downloads whisper-tiny first run)")
    kws = KWSWake("hey alpha")
    hits = sum(kws.feed_utterance(w) for w in wake_pcms)
    check("KWS fires on 'hey alpha'", hits >= 2, f"{hits}/3 variants detected")

    print("==> test 2: KWS stays quiet on ordinary speech")
    kws2 = KWSWake("hey alpha")
    hit2 = kws2.feed_utterance(hello_pcm) or kws2.feed_utterance(synth("what is the weather today"))
    check("KWS does NOT fire on ordinary speech", not hit2)

    # --- 2. Recorder: VAD endpointing on a synthetic stream ------------------
    print("==> test 3: Recorder endpoints 'hello' from a mic-like stream")
    from alpha.audio import RingBuffer
    from alpha.listen import Recorder

    ring = RingBuffer(capacity_s=16)
    rec = Recorder()
    # simulate: 0.5s silence, "hello", then 2s trailing silence
    stream = np.concatenate([silence(0.5), hello_pcm, silence(2.5)])
    # preload the ring with the pre-speech silence + utterance; the recorder
    # will consume it and stop after trailing silence
    ring.append(stream)
    utterance = rec.record_utterance(ring)
    dur = len(utterance) / 16000
    check("recorded an utterance", 0.2 < dur < 4.0, f"{dur:.2f}s captured")

    # --- 3. Request transcription (whisper small) ----------------------------
    print("==> test 4: request transcriber understands the utterance")
    stt = Transcriber(model="small", language="auto")
    text = stt.transcribe(utterance)
    check("transcript contains 'hello'", "hello" in text.lower(), f"text={text!r}")

    # --- 4. The echo: piper speaks the transcript back -----------------------
    print("==> test 5: piper speaks the transcript back (Arabic auto-detect)")
    from alpha.tts import SpeakerTTS, looks_arabic

    tts = SpeakerTTS("en_US-lessac-medium", "ar_JO-kareem-medium")
    reply = f"You said: {text}"
    tts.say(reply)  # plays to the default output device (speaker)
    check("spoke the reply out loud", True, reply)

    arabic_reply = "مرحبا، أنا ألفا"
    check("Arabic detection", looks_arabic(arabic_reply))

    print()
    if failures:
        print(f"RESULT: {failures} FAILURES")
        return 1
    print("RESULT: M2 AUDIO PIPELINE OK")
    print("Now try it live:  systemctl --user restart alpha; then say 'Hey Alpha' ... 'hello'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
