"""The Alpha daemon: a background state machine.

State machine (IDLE -> LISTENING -> THINKING -> ACTING -> SPEAKING -> IDLE).
For M1 only IDLE is wired; later milestones add the real transitions. The
daemon's job right now is to load config, log "idle" and stay alive so the
systemd --user unit can be verified.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from enum import Enum

from .config import Config, load_config

log = logging.getLogger(__name__)


class State(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    ACTING = "acting"
    SPEAKING = "speaking"
    MUTED = "muted"
    ERROR = "error"


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = State.IDLE
        self._stop = asyncio.Event()
        self._lazy = {}  # slot for lazily-loaded components (wake, stt, tts...)

    async def run(self) -> None:
        log.info("alpha-agent daemon starting (assistant=%r, state=%s)",
                 self.cfg.assistant.name, self.state.value)
        self._install_signal_handlers()
        # M1: idle forever until SIGTERM/SIGINT. The wake loop lands in M2.
        try:
            await self._stop.wait()
        finally:
            log.info("alpha-agent daemon stopping (state=%s)", self.state.value)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:  # pragma: no cover
                pass

    def transition(self, new: State) -> None:
        log.info("state %s -> %s", self.state.value, new.value)
        self.state = new


async def main_async() -> int:
    try:
        cfg = load_config()
    except Exception as e:
        log.error("failed to load config: %s", e)
        return 1
    daemon = Daemon(cfg)
    await daemon.run()
    return 0