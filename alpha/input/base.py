"""Input backends (M4). Pluggable: X11 (pyautogui) vs Wayland (ydotool/uinput)."""

from __future__ import annotations

from abc import ABC, abstractmethod


class InputBackend(ABC):
    """Absolute-coordinate desktop input. Coordinates are GLOBAL screen space."""

    @abstractmethod
    def move_abs(self, x: int, y: int) -> None: ...

    @abstractmethod
    def click(self, x: int, y: int, button: str = "left") -> None: ...

    @abstractmethod
    def type_text(self, text: str) -> None: ...

    @abstractmethod
    def key(self, combo: str) -> None: ...

    @abstractmethod
    def scroll(self, dx: int, dy: int) -> None: ...

    @abstractmethod
    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None: ...

    @abstractmethod
    def get_cursor_pos(self) -> tuple[int, int]: ...


def detect_backend(session_type: str) -> InputBackend:
    raise NotImplementedError("M4")
