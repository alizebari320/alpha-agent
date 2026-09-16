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
import os
import signal
import time
from enum import StrEnum

import numpy as np

from .audio import FRAME_SAMPLES, Mic, Speaker, ack_sound, done_sound, error_sound
from .config import Config, load_config
from .ipc import CTL_SOCK, HUD_SOCK, CtlServer, LineServer, spawn_hud

log = logging.getLogger(__name__)


class State(StrEnum):
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
        self._agent = None  # lazy AgentLoop (M6)

        # Fire-and-forget tasks (sounds, HUD re-spawn) are held here so the
        # event loop cannot garbage-collect them mid-flight.
        self._bg_tasks: set[asyncio.Task] = set()

        # IPC (HUD + control socket)
        self.hud = LineServer(HUD_SOCK)
        self.ctl = CtlServer(CTL_SOCK, on_command=self._handle_command)

        # Safety guard (§10): abort + allowlist + audit — created BEFORE any
        # input backend exists (spec hard constraint: abort path first).
        from .safety.guard import SafetyGuard

        self.guard = SafetyGuard(cfg.safety)

        # Vision worker (M5): talks to the HUD subprocess for
        # screenshots/AT-SPI; Pillow downscale/SoM locally.
        from .vision import DesktopWorker, Vision

        self.worker = DesktopWorker()
        self.worker.attach(self)
        self.vision = Vision(self.worker, self,
                             password_lock=cfg.safety.password_field_lock,
                             max_width=cfg.vision.max_width)

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
        the spec, only the wake-word model may live at idle.
        """
        if self._stt is not None:
            self._stt.unload()
            self._stt = None
        if self._tts is not None:
            self._tts.unload()
            self._tts = None
        _release_free_memory()
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
            # Keep a strong reference: a bare create_task can be garbage
            # collected mid-flight (RUF006), which would cut the sound off.
            self._bg_tasks.add(task := asyncio.create_task(
                asyncio.to_thread(self._play_and_wait, pcm, sr)))
            task.add_done_callback(self._bg_tasks.discard)
            return {"ok": True, "aborted": True}
        if cmd == "ask":
            # text mode (`alpha ask "..."`): the full plan+act path with no mic,
            # used for testing and for users who prefer typing.
            text = (msg.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "empty request"}
            if self._busy:
                return {"ok": False, "error": "busy"}
            self._busy = True
            try:
                answer = await self._run_request(text, speak=not msg.get("no_speak"))
            finally:
                self._busy = False
                self.transition(State.IDLE)
            return {"ok": True, "answer": answer}
        if cmd == "warm":
            # Pre-load the request models so the first request is fast, and so
            # the RAM table in docs/PERFORMANCE.md can be measured honestly.
            before = _rss_mb()

            def _load_all() -> None:
                self._get_stt().preload()
                self._get_tts().preload()
                self._get_input()
                self._get_recorder()

            await asyncio.to_thread(_load_all)
            self._last_busy = time.time()
            return {"ok": True, "rss_mb": _rss_mb(), "children_mb": _children_rss_mb(),
                    "delta_mb": _rss_mb() - before}
        if cmd == "unload":
            await asyncio.to_thread(self._unload_heavy)
            return {"ok": True, "rss_mb": _rss_mb(), "children_mb": _children_rss_mb()}
        if cmd == "res":
            # vision worker reply (correlated by id)
            self.worker.resolve(msg)
            return None  # no reply needed on ctl
        if cmd == "vision-test":
            # manual M5 check: screenshot + atspi + password lock.
            # `ok` means "the request was answered", NOT "a screenshot was
            # captured" — without that distinction a missing portal permission
            # surfaced to the user as "daemon not reachable", which sent people
            # hunting for a socket bug instead of clicking Share.
            shot = await self.vision.screenshot()
            els = await self.vision.atspi()
            locked = await self.vision.check_password_lock(els)
            return {"ok": True, "screenshot": bool(shot),
                    "shot": f"{shot.width}x{shot.height}" if shot else None,
                    "atspi_elements": len(els), "password_locked": locked}
        if cmd == "set-geom":
            # HUD reports the real monitor layout (GTK sees what the
            # compositor sees) — used by input/vision for coordinate math.
            self.monitors = msg.get("monitors", [])
            if self.monitors:
                w = max(m["x"] + m["w"] for m in self.monitors)
                h = max(m["y"] + m["h"] for m in self.monitors)
                self.screen_size = (w, h)
            log.info("geometry: monitors=%s screen=%s", self.monitors, self.screen_size)
            # Pre-warm the input backend now: virtual devices need a moment
            # before Mutter accepts events (see alpha/input/uinput.py). Paying
            # that cost at startup keeps the first real command instant.
            try:
                await asyncio.to_thread(self._get_input)
            except Exception as e:
                log.warning("input backend not ready: %s", e)
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
            self._hud_state("…")
            text = await asyncio.to_thread(self._get_stt().transcribe, request)
            log.info("request transcript: %r", text)
            if not text:
                await self._say_error("Sorry, I couldn't understand that.")
                return
            self._hud_state(text)

            # strip a leading wake phrase if the user said it in one breath
            ww = self.cfg.assistant.wake_word.phrase.lower()
            if text.lower().startswith(ww):
                text = text[len(ww):].strip(" ,.-")

            await self._run_request(text)
        finally:
            self.transition(State.IDLE)
            self.mic.clear()
            self._last_busy = time.time()
            self._busy = False

    async def _run_request(self, text: str, speak: bool = True) -> str:
        """Plan + act + answer for one already-transcribed request.

        Shared by the voice path and by `alpha ask` (text mode), so the whole
        pipeline can be exercised without a microphone.
        """
        from .safety.guard import AbortRequested

        # A new request begins: clear any stale abort from the previous one
        # (aborts during idle apply to nothing; §10 semantics).
        self.guard.clear_abort()
        try:
            self.transition(State.ACTING)
            self._hud_state(text[:80])
            answer = await self._get_agent().run(text)
        except AbortRequested:
            self.transition(State.IDLE)
            await self.speak("Aborted.")
            return "Aborted."
        except Exception as e:
            log.exception("agent loop failed")
            # §11.5: spoken message, never a stack trace
            answer = self._friendly_error(e)

        self.transition(State.SPEAKING)
        self._hud_state(answer[:120])
        if speak:
            await asyncio.to_thread(self._say, answer)
            pcm, sr = done_sound()
            await asyncio.to_thread(self._play_and_wait, pcm, sr)
        return answer

    def _play_and_wait(self, pcm: np.ndarray, sr: int) -> None:
        self.speaker.play_pcm(pcm, sr)
        self.speaker.wait_done()

    def _say(self, text: str) -> None:
        try:
            self._get_tts().say(text)
        except Exception as e:
            log.error("TTS failed: %s", e)

    # async speak (used by the agent loop + confirmations)
    async def speak(self, text: str) -> None:
        await asyncio.to_thread(self._say, text)

    async def record_request(self):
        return await asyncio.to_thread(
            self._get_recorder().record_utterance, self.mic.ring)

    async def transcribe(self, pcm) -> str:
        return await asyncio.to_thread(self._get_stt().transcribe, pcm)

    def _get_agent(self):
        if self._agent is None:
            from .brain.loop import AgentLoop

            self._agent = AgentLoop(self)
        return self._agent

    def _friendly_error(self, e: Exception) -> str:
        """§11.5: map failure classes to SPOKEN messages, never stack traces."""
        import httpx

        msg = str(e).lower()
        if isinstance(e, httpx.ConnectError) or "connection" in msg or "network" in msg:
            return "I can't reach my model provider — the internet seems down."
        if "quota" in msg or "insufficient" in msg or "credit" in msg or "billing" in msg:
            return ("My API credit is used up, so I can't think right now. "
                    "Add credit to the provider account or switch providers in the config.")
        if "is not available on this account" in msg:
            return ("The model I'm configured to use isn't available on this account. "
                    "Run alpha doctor to see which models you can use.")
        if "401" in msg or "unauthorized" in msg or ("invalid" in msg and "key" in msg):
            return "My API key was rejected. Please re-run alpha init."
        if "429" in msg or "rate" in msg:
            return "The model provider is rate limiting me. Try again in a minute."
        if "502" in msg or "503" in msg or "504" in msg or "timeout" in msg:
            return "The model provider is having an outage. Try again shortly."
        if "not installed" in msg or "no such file" in msg or "command not found" in msg:
            return "That application isn't installed."
        return "Something went wrong on my side — check the logs for details."

    async def _say_error(self, msg: str) -> None:
        pcm, sr = error_sound()
        await asyncio.to_thread(self._play_and_wait, pcm, sr)
        await asyncio.to_thread(self._say, msg)
        self._hud_state(msg)



def _release_free_memory() -> None:
    """Give freed arenas back to the OS.

    Dropping the last reference to a whisper model does NOT lower RSS: glibc
    keeps the freed arenas and onnxruntime keeps its own pool (measured: an
    unload that changed nothing). malloc_trim hands them back, which is what
    makes the idle-unload promise in the spec actually true.
    """
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:
        pass


def _proc_rss_mb(pid: int) -> float:
    """Resident set size of one process in MiB (0.0 if it is gone)."""
    try:
        with open(f"/proc/{pid}/statm") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return 0.0


def _rss_mb() -> float:
    """Resident set size of this daemon in MiB."""
    return _proc_rss_mb(os.getpid())


def _children_rss_mb() -> float:
    """Combined RSS of our child processes (the GTK HUD) in MiB."""
    total = 0.0
    try:
        with open(f"/proc/{os.getpid()}/task/{os.getpid()}/children") as f:
            for cpid in f.read().split():
                total += _proc_rss_mb(int(cpid))
    except OSError:
        pass
    return total


async def main_async() -> int:
    try:
        cfg = load_config()
    except Exception as e:
        log.error("failed to load config: %s", e)
        return 1
    daemon = Daemon(cfg)
    await daemon.run()
    return 0
