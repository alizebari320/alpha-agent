"""X11 input backend via xdotool (subprocess, no extra deps)."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time

from .base import InputBackend

log = logging.getLogger(__name__)


class X11Backend(InputBackend):
    """Absolute-coordinate input on X11 via the xdotool binary."""

    def __init__(self):
        if shutil.which("xdotool") is None:
            raise RuntimeError("xdotool not installed (dnf install xdotool)")
        self._pos = (0, 0)

    def _run(self, *args: str) -> str:
        r = subprocess.run(["xdotool", *args], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise RuntimeError(f"xdotool {' '.join(args)} failed: {r.stderr.strip()}")
        return r.stdout.strip()

    def move_abs(self, x: int, y: int, interpolate: bool = True) -> None:
        # xdotool mousemove is absolute by default; interpolate for hover
        if interpolate:
            x0, y0 = self.get_cursor_pos()
            steps = max(1, min(12, int(((x - x0) ** 2 + (y - y0) ** 2) ** 0.5 // 60)))
            for i in range(1, steps + 1):
                self._run("mousemove", str(x0 + (x - x0) * i // steps),
                          str(y0 + (y - y0) * i // steps))
                time.sleep(0.008)
        else:
            self._run("mousemove", str(int(x)), str(int(y)))
        self._pos = (int(x), int(y))

    def click(self, x: int, y: int, button: str = "left") -> None:
        btn = {"left": "1", "middle": "2", "right": "3"}[button]
        self.move_abs(x, y)
        time.sleep(0.02)
        self._run("click", btn)

    def double_click(self, x: int, y: int, button: str = "left") -> None:
        btn = {"left": "1", "middle": "2", "right": "3"}[button]
        self.move_abs(x, y)
        self._run("click", "--repeat", "2", "--delay", "80", btn)

    def right_click(self, x: int, y: int) -> None:
        self.click(x, y, "right")

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
        btn = {"left": "1", "middle": "2", "right": "3"}[button]
        self.move_abs(x1, y1)
        self._run("mousedown", btn)
        self.move_abs(x2, y2)
        time.sleep(0.05)
        self._run("mouseup", btn)

    def scroll(self, dx: int, dy: int) -> None:
        if dy:
            self._run("click", "--repeat", str(abs(int(dy))), "5" if dy > 0 else "4")
        if dx:
            self._run("click", "--repeat", str(abs(int(dx))), "7" if dx > 0 else "6")

    def type_text(self, text: str) -> None:
        self._run("type", "--", text)

    def key(self, combo: str) -> None:
        self._run("key", combo)

    def get_cursor_pos(self) -> tuple[int, int]:
        out = self._run("getmouselocation", "--shell")
        d = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k] = int(v)
        self._pos = (d.get("X", 0), d.get("Y", 0))
        return self._pos
