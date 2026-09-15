"""Safety guard (M4): abort hotkey, allowlist, password-field lock, denylist."""

from __future__ import annotations


class SafetyGuard:
    def __init__(self, abort_hotkey: str = "ctrl+alt+q"):
        self.abort_hotkey = abort_hotkey

    def start(self) -> None:
        raise NotImplementedError("M4")