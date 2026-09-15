"""The Alpha daemon: background state machine + the M2 audio pipeline.

States: IDLE -> LISTENING -> THINKING -> (ACTING) -> SPEAKING -> IDLE.

M2 wiring:
  IDLE      mic open, wake word matcher active (pretrained or kws)
  LISTENING ack sound, record_utterance()
  THINKING  whisper transcribes (M6 adds the LLM planner/executor)
  SPEAKING  piper speaks

Blocking audio/ML work runs in threads (asyncio.to_thread) so the event loop
stays responsive for HUD/hotkeys in later milestones. Everything heavy is
lazy-loaded; after idle_unload_s it is freed again (§9).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from enum import Enum

import numpy as np

from .audio import FRAME_SAMPLES, Mic, Speaker, ack_sound, error_sound, done_sound
from .config import Config, load_config
from .ipc import CTL_SOCK, HUD_SOCK, CtlServer, LineServer, spawn_hud

log = logging.getLogger(__name__)


class State(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    ACTING = "acting"
    SPEAKING = "speaking"
    MUTED = "muted"
    ERROR = "error"


KWS_WINDOW_S = 2.0      # rolling KWS window (§8 Tier 3)
KWS_CHECK_EVERY_S = 1.5  # how often the window is re-decoded


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = State.IDLE
        self._stop = asyncio.Event()

        # Desktop geometry (reported by the HUD process via GTK)
        self.monitors: list[dict] = []
        self.screen_size: tuple[int, int] | None = None
        self.input = None  # lazy InputBackend

        # Audio (mic is the only eager component — everything else lazy, §9)
        self.mic = Mic()
        self.speaker = Speaker()

        # Lazy components
        self._wake = None      # PretrainedWake | KWSWake
        self._stt = None       # Transcriber
        self._tts = None       # SpeakerTTS
        self._recorder = None  # Recorder
        self._last_busy = time.time()

        # KWS rolling-window state
        self._kws_buf = np.zeros(0, dtype=np.int16)
        self._kws_since_check = 0.0
        self._kws_last_hit = 0.0
        self._busy = False
        self._muted = False

        # IPC (HUD + control socket)
        self.hud = LineServer(HUD_SOCK)
        self.ctl = CtlServer(CTL_SOCK, on_command=self._handle_command)

        # Safety guard (§10): abort + allowlist + audit — created BEFORE any
        # input backend exists (spec hard constraint: abort path first).
        from .safety.guard import SafetyGuard

        self.guard = SafetyGuard(cfg.safety)

    # ---------------- lazy loaders ----------------

    def _get_wake(self):
        if self._wake is None:
            w = self.cfg.assistant.wake_word
            if w.mode == "kws":
                from .wake import KWSWake

                self._wake = KWSWake(w.phrase, threshold=w.threshold)
            else:
                from .wake import PretrainedWake

                self._wake = PretrainedWake(w.model, threshold=w.threshold,
                                            refractory_s=w.refractory_s)
        return self._wake

    def _get_stt(self):
        if self._stt is None:
            from .stt import Transcriber

            self._stt = Transcriber(self.cfg.stt.model, self.cfg.stt.language)
        return self._stt

    def _get_tts(self):
        if self._tts is None:
            from .tts import SpeakerTTS

            self._tts = SpeakerTTS(self.cfg.tts.voice, self.cfg.tts.arabic_voice,
                                   self.cfg.tts.volume, self.speaker)
        return self._tts

    def _get_recorder(self):
        if self._recorder is None:
            from .listen import Recorder

            self._recorder = Recorder()
        return self._recorder

    def _get_input(self):
        """Lazy input backend using the HUD-reported desktop geometry."""
        if self.input is None:
            from .input.detect import detect_backend

            w, h = self.screen_size or (1920, 1080)
            self.input = detect_backend(w, h)
            log.info("input backend ready: %s", type(self.input).__name__)
        return self.input

    def _unload_heavy(self) -> None:
        """Idle timeout: free the REQUEST transcriber + piper (§9).

        The wake matcher (whisper-tiny or openwakeword) stays resident — per
        the spec, only the wake-word model may live at idle."""
        if self._stt is not None:
            self._stt.unload()
            self._stt = None
        if self._tts is not None:
            self._tts.unload()
            self._tts = None
        log.info("idle unload: freed whisper/piper (wake matcher stays resident)")

    # ---------------- mute (§10: the mic is genuinely RELEASED) ------------

    async def _handle_command(self, msg: dict) -> dict | None:
        cmd = msg.get("cmd")
        if cmd == "mute-toggle":
            await self.set_muted(not self._muted)
            return {"ok": True, "muted": self._muted}
        if cmd == "mute":
            await self.set_muted(True)
            return {"ok": True, "muted": self._muted}
        if cmd == "unmute":
            await self.set_muted(False)
            return {"ok": True, "muted": self._muted}
        if cmd == "state":
            return {"ok": True, "state": self.state.value, "muted": self._muted,
                    "assistant": self.cfg.assistant.name,
                    "monitors": self.monitors, "screen": self.screen_size}
        if cmd == "abort":
            # §10: Ctrl+Alt+Q equivalent — kill any running loop instantly.
            self.guard.request_abort("ctl")
            self._busy = False
            self.transition(State.IDLE)
            pcm, sr = error_sound()
            asyncio.create_task(asyncio.to_thread(self._play_and_wait, pcm, sr))
            return {"ok": True, "aborted": True}
        if cmd == "set-geom":
            # HUD reports the real monitor layout (GTK sees what the
            # compositor sees) — used by input/vision for coordinate math.
            self.monitors = msg.get("monitors", [])
            if self.monitors:
                w = max(m["x"] + m["w"] for m in self.monitors)
                h = max(m["y"] + m["h"] for m in self.monitors)
                self.screen_size = (w, h)
            log.info("geometry: monitors=%s screen=%s", self.monitors, self.screen_size)
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    async def set_muted(self, muted: bool) -> None:
        self._muted = muted
        if muted:
            self.mic.stop(release_device=True)  # genuinely release the h/w mic
            self.transition(State.MUTED)
        else:
            self.mic.start()
            self.transition(State.IDLE)
        self.hud.broadcast({"type": "mute", "muted": muted})
        log.info("muted=%s (mic %s)", muted, "released" if muted else "open")

    # ---------------- HUD ----------------

    def _hud_state(self, text: str = "") -> None:
        self.hud.broadcast({"type": "state", "state": self.state.value, "text": text})

    def transition(self, new: State) -> None:
        log.info("state %s -> %s", self.state.value, new.value)
        self.state = new
        self._hud_state("")

    # ---------------- main loop ----------------

    async def run(self) -> None:
        log.info("alpha-agent daemon starting (assistant=%r, state=%s)",
                 self.cfg.assistant.name, self.state.value)
        self._install_signal_handlers()
        await self.hud.start()
        await self.ctl.start()
        self._hud_proc = spawn_hud()
        self.mic.start()
        log.info("wake word mode=%s phrase=%r — listening",
                 self.cfg.assistant.wake_word.mode, self.cfg.assistant.wake_word.phrase)
        try:
            await self._main()
        finally:
            log.info("alpha-agent daemon stopping (state=%s)", self.state.value)
            self.speaker.stop()
            self.mic.stop(release_device=True)
            if getattr(self, "_hud_proc", None):
                try:
                    self._hud_proc.terminate()
                except Exception:
                    pass

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:  # pragma: no cover
                pass

    async def _main(self) -> None:
        from .wake import KWSWake

        while not self._stop.is_set():
            if self._busy or self._muted:
                await asyncio.sleep(0.05)
                continue

            # Idle-unload: free heavy models after cfg.resources.idle_unload_s.
            if (self.state == State.IDLE and self._stt is not None
                    and (time.time() - self._last_busy) > self.cfg.resources.idle_unload_s):
                self._unload_heavy()

            frame = self.mic.pop_samples(FRAME_SAMPLES)
            if frame is None:
                await asyncio.sleep(0.005)
                continue

            wake = self._get_wake()
            if isinstance(wake, KWSWake):
                heard = await self._kws_step(wake, frame)
            else:
                heard = wake.feed(frame)

            if heard:
                await self._handle_wake()

    async def _kws_step(self, wake, frame: np.ndarray) -> bool:
        """Maintain a rolling 2 s window; every KWS_CHECK_EVERY_S, transcribe it
        and fuzzy-match the wake phrase (§8 Tier 3)."""
        self._kws_buf = np.concatenate([self._kws_buf, frame])
        max_samples = int(KWS_WINDOW_S * 16000)
        if len(self._kws_buf) > max_samples:
            self._kws_buf = self._kws_buf[-max_samples:]
        self._kws_since_check += len(frame) / 16000.0
        if self._kws_since_check < KWS_CHECK_EVERY_S:
            return False
        self._kws_since_check = 0.0
        if len(self._kws_buf) < max_samples * 0.9:
            return False
        # refractory: don't retrigger immediately after a hit
        if (time.time() - self._kws_last_hit) < 5.0:
            return False

        window = self._kws_buf.copy()

        # VAD gate (spec §8 Tier 3): skip whisper entirely when the window is
        # just silence. This is what keeps idle CPU inside the §9 budget —
        # whisper only runs when someone is actually speaking.
        recorder = self._get_recorder()
        ratio = await asyncio.to_thread(recorder.speech_ratio, window)
        if ratio < 0.15:
            return False

        heard = await asyncio.to_thread(wake.feed_utterance, window)
        if heard:
            self._kws_last_hit = time.time()
            self._kws_buf = np.zeros(0, dtype=np.int16)
        return heard

    async def _handle_wake(self) -> None:
        self._busy = True
        self._last_busy = time.time()
        try:
            self.transition(State.LISTENING)
            self._hud_state("Listening…")

            pcm, sr = ack_sound()
            await asyncio.to_thread(self._play_and_wait, pcm, sr)

            # Let the ack sound finish; drop pre-wake audio but keep whatever
            # the user is saying RIGHT NOW (the ring keeps filling from the mic).
            await asyncio.sleep(0.1)
            self.mic.clear()

            # 1) record the request
            request = await asyncio.to_thread(
                self._get_recorder().record_utterance, self.mic.ring
            )
            if len(request) < 1600:  # <100ms
                await self._say_error("I didn't catch anything; try again.")
                return

            # 2) transcribe
            self.transition(State.THINKING)
            self._hud_state(text or "…")
            text = await asyncio.to_thread(self._get_stt().transcribe, request)
            log.info("request transcript: %r", text)
            if not text:
                await self._say_error("Sorry, I couldn't understand that.")
                return

            # 3) M2 acceptance: echo the transcript back.
            self.transition(State.SPEAKING)
            self._hud_state(text)
            await asyncio.to_thread(self._say, text)
            pcm, sr = done_sound()
            await asyncio.to_thread(self._play_and_wait, pcm, sr)
        finally:
            self.transition(State.IDLE)
            self.mic.clear()
            self._last_busy = time.time()
            self._busy = False

    def _play_and_wait(self, pcm: np.ndarray, sr: int) -> None:
        self.speaker.play_pcm(pcm, sr)
        self.speaker.wait_done()

    def _say(self, text: str) -> None:
        try:
            self._get_tts().say(text)
        except Exception as e:
            log.error("TTS failed: %s", e)

    async def _say_error(self, msg: str) -> None:
        pcm, sr = error_sound()
        await asyncio.to_thread(self._play_and_wait, pcm, sr)
        await asyncio.to_thread(self._say, msg)
        self._hud_state(msg)


async def main_async() -> int:
    try:
        cfg = load_config()
    except Exception as e:
        log.error("failed to load config: %s", e)
        return 1
    daemon = Daemon(cfg)
    await daemon.run()
    return 0
