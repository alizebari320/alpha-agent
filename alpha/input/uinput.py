"""Native /dev/uinput input backend — kernel-level virtual mouse+keyboard.

This is the SAME mechanism ydotool/ydotoold uses (a uinput virtual device),
implemented directly in Python so no daemon, dnf package, or root access is
needed when the user is in the `input` group. Works on BOTH Wayland and X11
(uinput sits below the display server).

Absolute positioning: the device is created with ABS_X/ABS_Y axes sized to the
full desktop, so we inject exact global coordinates (no relative-warp needed).
"""

from __future__ import annotations

import logging
import os
import time

from . import evdev
from .base import InputBackend

log = logging.getLogger(__name__)

UINPUT_PATH = "/dev/uinput"


class UInputError(Exception):
    pass


class UInputBackend(InputBackend):
    """Mouse+keyboard via one uinput virtual device."""

    def __init__(self, screen_w: int, screen_h: int):
        self.screen_w = int(screen_w)
        self.screen_h = int(screen_h)
        self._pos = (self.screen_w // 2, self.screen_h // 2)  # tracked position
        self._fd: int | None = None
        self._open()

    # ---------------------------------------------------------------- setup

    def _open(self) -> None:
        try:
            self._fd = os.open(UINPUT_PATH, os.O_WRONLY | os.O_NONBLOCK)
        except PermissionError as e:
            raise UInputError(
                f"no permission on {UINPUT_PATH}: add yourself to the 'input' "
                f"group (sudo usermod -aG input $USER, then re-login) — {e}"
            ) from e
        except FileNotFoundError as e:
            raise UInputError(
                f"{UINPUT_PATH} not found: load the module (sudo modprobe uinput)"
            ) from e

        fcntl_ioctl = __import__("fcntl").ioctl
        fd = self._fd
        try:
            # event types
            for bit in (evdev.EV_KEY, evdev.EV_REL, evdev.EV_ABS, evdev.EV_SYN):
                fcntl_ioctl(fd, evdev.UI_SET_EVBIT, bit)
            # mouse buttons + every key we may emit
            for btn in evdev.BUTTONS.values():
                fcntl_ioctl(fd, evdev.UI_SET_KEYBIT, btn)
            for code in set(evdev.KEYMAP.values()):
                fcntl_ioctl(fd, evdev.UI_SET_KEYBIT, code)
            # relative axes (wheel) + absolute axes (exact positioning)
            for rel in (evdev.REL_X, evdev.REL_Y, evdev.REL_WHEEL, evdev.REL_HWHEEL):
                fcntl_ioctl(fd, evdev.UI_SET_RELBIT, rel)
            for ab in (evdev.ABS_X, evdev.ABS_Y):
                fcntl_ioctl(fd, evdev.UI_SET_ABSBIT, ab)

            dev = evdev.uinput_user_dev(
                name=b"alpha-agent virtual input",
                bustype=0x03,  # BUS_USB
                vendor=0x1234, product=0x5678, version=1,
                absmax={evdev.ABS_X: max(1, self.screen_w - 1),
                        evdev.ABS_Y: max(1, self.screen_h - 1)},
            )
            os.write(fd, dev)
            fcntl_ioctl(fd, evdev.UI_DEV_CREATE)
        except OSError as e:
            os.close(fd)
            self._fd = None
            raise UInputError(f"uinput device setup failed: {e}") from e
        # give the compositor a beat to register the new device
        time.sleep(0.25)
        log.info("uinput device created (%dx%d absolute mouse+keyboard)",
                 self.screen_w, self.screen_h)

    def close(self) -> None:
        if self._fd is not None:
            try:
                __import__("fcntl").ioctl(self._fd, evdev.UI_DEV_DESTROY)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            log.info("uinput device destroyed")

    # ---------------------------------------------------------------- emit

    def _emit(self, type_: int, code: int, value: int) -> None:
        if self._fd is None:
            raise UInputError("uinput device is closed")
        now = time.time()
        s, us = int(now), int((now % 1) * 1e6)
        os.write(self._fd, evdev.input_event(s, us, type_, code, value))

    def _syn(self) -> None:
        self._emit(evdev.EV_SYN, evdev.SYN_REPORT, 0)

    def _key_event(self, code: int, down: bool) -> None:
        self._emit(evdev.EV_KEY, code, 1 if down else 0)
        self._syn()

    # ---------------------------------------------------------------- API

    def move_abs(self, x: int, y: int, interpolate: bool = True) -> None:
        x = int(max(0, min(x, self.screen_w - 1)))
        y = int(max(0, min(y, self.screen_h - 1)))
        if interpolate:
            # a few steps so UIs register hover (menus, tooltips)
            x0, y0 = self._pos
            steps = max(1, min(12, int(((x - x0) ** 2 + (y - y0) ** 2) ** 0.5 // 60)))
            for i in range(1, steps + 1):
                xi = x0 + (x - x0) * i // steps
                yi = y0 + (y - y0) * i // steps
                self._emit(evdev.EV_ABS, evdev.ABS_X, xi)
                self._emit(evdev.EV_ABS, evdev.ABS_Y, yi)
                self._syn()
                time.sleep(0.008)
        else:
            self._emit(evdev.EV_ABS, evdev.ABS_X, x)
            self._emit(evdev.EV_ABS, evdev.ABS_Y, y)
            self._syn()
        self._pos = (x, y)

    def click(self, x: int, y: int, button: str = "left") -> None:
        if button not in evdev.BUTTONS:
            raise ValueError(f"unknown button {button!r}")
        self.move_abs(x, y)
        time.sleep(0.02)
        btn = evdev.BUTTONS[button]
        self._key_event(btn, True)
        time.sleep(0.02)
        self._key_event(btn, False)

    def double_click(self, x: int, y: int, button: str = "left") -> None:
        self.click(x, y, button)
        time.sleep(0.08)
        self._key_event(evdev.BUTTONS[button], True)
        time.sleep(0.02)
        self._key_event(evdev.BUTTONS[button], False)

    def right_click(self, x: int, y: int) -> None:
        self.click(x, y, "right")

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
        btn = evdev.BUTTONS.get(button, evdev.BTN_LEFT)
        self.move_abs(x1, y1)
        time.sleep(0.05)
        self._key_event(btn, True)
        self.move_abs(x2, y2)
        time.sleep(0.05)
        self._key_event(btn, False)

    def scroll(self, dx: int, dy: int) -> None:
        if dy:
            self._emit(evdev.EV_REL, evdev.REL_WHEEL, int(dy))
        if dx:
            self._emit(evdev.EV_REL, evdev.REL_HWHEEL, int(dx))
        self._syn()

    def type_text(self, text: str) -> None:
        for ch in text:
            kp = evdev.char_to_key(ch)
            if kp is None:
                log.warning("untypable char %r skipped", ch)
                continue
            code, needs_shift = kp
            if needs_shift:
                self._key_event(evdev.KEY_LEFTSHIFT, True)
            self._key_event(code, True)
            time.sleep(0.012)
            self._key_event(code, False)
            if needs_shift:
                self._key_event(evdev.KEY_LEFTSHIFT, False)
            time.sleep(0.012)

    def key(self, combo: str) -> None:
        """e.g. 'ctrl+l', 'Return', 'alt+Tab' (xdotool-style names)."""
        parts = [p.strip() for p in combo.split("+") if p.strip()]
        if not parts:
            return
        codes = []
        for p in parts:
            if p not in evdev.KEYMAP:
                raise ValueError(f"unknown key {p!r} in combo {combo!r}")
            codes.append(evdev.KEYMAP[p])
        for c in codes:
            self._key_event(c, True)
            time.sleep(0.012)
        for c in reversed(codes):
            self._key_event(c, False)
            time.sleep(0.012)

    def get_cursor_pos(self) -> tuple[int, int]:
        # uinput devices can't be queried; we track what we injected. External
        # moves (human) are unknown — acceptable for M4 (documented).
        return self._pos
