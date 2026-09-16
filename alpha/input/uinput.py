"""Native /dev/uinput input backend — kernel-level virtual pointer + keyboard.

Same mechanism ydotool uses, implemented directly in Python (no daemon, no dnf
package, no root beyond the `input` group). Works on Wayland AND X11 because
uinput sits below the display server.

Two hard-won lessons, both verified empirically on GNOME/Mutter (Wayland):

1. ONE DEVICE PER KIND. A kitchen-sink device that mixes keys + buttons +
   REL + ABS gets classified by udev/libinput as a JOYSTICK (`js0`) and Mutter
   then ignores it as a pointer. So we create exactly two clean devices:
     * alpha-agent pointer  — EV_ABS (ABS_X/ABS_Y) + mouse buttons + wheel
     * alpha-agent keyboard — letters/digits/function keys/modifiers

2. ABSOLUTE AXES, NOT RELATIVE WARPS. Mutter ignores rel-warp guesses, and
   pointer acceleration makes REL deltas land unpredictably. A device that
   advertises EV_ABS + `INPUT_PROP_POINTER` is treated by libinput as an
   absolute pointer: mutter maps ABS_X/ABS_Y 1:1 onto the screen with **no
   acceleration**, pixel-exact (measured with a GTK4 probe: requested
   (1500,900) -> pointer at (1500,900)). No xdotool, no position readback,
   no corner-anchoring heuristics.

3. THE DEVICE NEEDS A MOMENT TO BE PICKED UP. Mutter adds hotplugged input
   devices asynchronously from its main loop; events sent immediately after
   UI_DEV_CREATE are dropped. We therefore wait for the kernel's event node to
   appear *and* for a short settle period (see `READY_SETTLE_S`) before the
   backend is handed to the agent loop. Create the backend once (the daemon
   does) and reuse it.
"""

from __future__ import annotations

import fcntl
import logging
import os
import time

from . import evdev
from .base import InputBackend

log = logging.getLogger(__name__)

UINPUT_PATH = "/dev/uinput"
SYSFS_INPUT = "/sys/devices/virtual/input"
DEVINPUT = "/dev/input"

# Empirically required on GNOME/Mutter: events sent within ~2.5s of device
# creation can be dropped because mutter is still adding the device.
READY_SETTLE_S = 2.5


class UInputError(Exception):
    pass


class _Device:
    """One clean uinput device."""

    def __init__(self, name: bytes, evbits: list[int], keybits: list[int] = (),
                 relbits: list[int] = (), absbits: list[int] = (),
                 absmax: dict[int, int] | None = None,
                 props: list[int] = ()):
        try:
            self.fd = os.open(UINPUT_PATH, os.O_WRONLY | os.O_NONBLOCK)
        except PermissionError as e:
            raise UInputError(
                f"no permission on {UINPUT_PATH}: add yourself to the 'input' "
                f"group (sudo usermod -aG input $USER, then re-login) — {e}"
            ) from e
        except FileNotFoundError as e:
            raise UInputError(
                f"{UINPUT_PATH} not found: load the module (sudo modprobe uinput)"
            ) from e
        for b in evbits:
            fcntl.ioctl(self.fd, evdev.UI_SET_EVBIT, b)
        for b in keybits:
            fcntl.ioctl(self.fd, evdev.UI_SET_KEYBIT, b)
        for b in relbits:
            fcntl.ioctl(self.fd, evdev.UI_SET_RELBIT, b)
        for b in absbits:
            fcntl.ioctl(self.fd, evdev.UI_SET_ABSBIT, b)
        for p in props:
            fcntl.ioctl(self.fd, evdev.UI_SET_PROPBIT, p)
        dev = evdev.uinput_user_dev(
            name=name, bustype=0x03, vendor=0x1234, product=0x5678, version=1,
            absmax=absmax or {},
        )
        os.write(self.fd, dev)
        fcntl.ioctl(self.fd, evdev.UI_DEV_CREATE)
        self.name = name.decode("utf-8", "replace")

    def emit(self, type_: int, code: int, value: int) -> None:
        now = time.time()
        s, us = int(now), int((now % 1) * 1e6)
        os.write(self.fd, evdev.input_event(s, us, type_, code, value))

    def syn(self) -> None:
        self.emit(evdev.EV_SYN, evdev.SYN_REPORT, 0)

    def close(self) -> None:
        try:
            fcntl.ioctl(self.fd, evdev.UI_DEV_DESTROY)
            os.close(self.fd)
        except OSError:
            pass


def _event_node_for(name: str, timeout: float = 5.0) -> str | None:
    """Resolve /dev/input/eventN for a freshly created virtual device."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for entry in sorted(os.listdir(SYSFS_INPUT)):
                if not entry.startswith("input"):
                    continue
                try:
                    with open(f"{SYSFS_INPUT}/{entry}/name") as fh:
                        if fh.read().strip() != name:
                            continue
                    for node in os.listdir(f"{SYSFS_INPUT}/{entry}"):
                        if node.startswith("event"):
                            path = f"{DEVINPUT}/{node}"
                            if os.path.exists(path):
                                return path
                except OSError:
                    continue
        except OSError:
            pass
        time.sleep(0.1)
    return None


class UInputBackend(InputBackend):
    """Pointer + keyboard via two clean uinput virtual devices.

    Absolute coordinates are reported straight to the kernel: the pointer
    device advertises ABS_X/ABS_Y spanning the full screen, so `move_abs` is
    exact on every compositor (Wayland included) with no acceleration.
    """

    def __init__(self, screen_w: int, screen_h: int, settle: float = READY_SETTLE_S):
        self.screen_w = int(screen_w)
        self.screen_h = int(screen_h)
        self._pos = (self.screen_w // 2, self.screen_h // 2)
        self._ready_nodes: list[str | None] = []
        created = time.time()

        self._pointer = _Device(
            b"alpha-agent pointer",
            evbits=[evdev.EV_KEY, evdev.EV_ABS, evdev.EV_REL, evdev.EV_SYN],
            keybits=list(evdev.BUTTONS.values()),
            absbits=[evdev.ABS_X, evdev.ABS_Y],
            relbits=[evdev.REL_WHEEL, evdev.REL_HWHEEL],
            absmax={evdev.ABS_X: self.screen_w - 1, evdev.ABS_Y: self.screen_h - 1},
            props=[evdev.INPUT_PROP_POINTER],
        )
        self._kbd = _Device(
            b"alpha-agent keyboard",
            evbits=[evdev.EV_KEY, evdev.EV_SYN],
            keybits=sorted(set(evdev.KEYMAP.values())),
        )
        self._ready_nodes = [
            _event_node_for(self._pointer.name, timeout=4.0),
            _event_node_for(self._kbd.name, timeout=4.0),
        ]

        # Wait out mutter's asynchronous device-add. This is a one-time cost paid
        # by the daemon at startup (or by a freshly spawned backend).
        leftover = settle - (time.time() - created)
        if leftover > 0:
            time.sleep(leftover)

        missing = [n for n in self._ready_nodes if not n]
        if missing:
            log.warning("uinput: %d device(s) not found in /dev/input yet", len(missing))
        log.info("uinput ready: absolute pointer + keyboard (%dx%d)%s",
                 self.screen_w, self.screen_h,
                 "" if not missing else " (degraded)")

    def close(self) -> None:
        self._pointer.close()
        self._kbd.close()
        log.info("uinput devices destroyed")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- position

    def get_cursor_pos(self) -> tuple[int, int]:
        """Last position we commanded.

        The kernel gives no readback for synthetic devices, and the Xwayland
        mirror is stale over Wayland surfaces — so we track. Absolute moves are
        deterministic, which is what the vision pipeline relies on.
        """
        return self._pos

    def _move_raw(self, x: int, y: int) -> None:
        self._pointer.emit(evdev.EV_ABS, evdev.ABS_X, x)
        self._pointer.emit(evdev.EV_ABS, evdev.ABS_Y, y)
        self._pointer.syn()

    def move_abs(self, x: int, y: int, interpolate: bool = True) -> None:
        """Move the pointer to exact global screen coordinates."""
        x = int(max(0, min(int(x), self.screen_w - 1)))
        y = int(max(0, min(int(y), self.screen_h - 1)))
        if not interpolate:
            self._move_raw(x, y)
            self._pos = (x, y)
            return
        # A short glide so hover/tooltip/menu UIs actually see motion.
        cx, cy = self._pos
        steps = max(1, min(8, int(max(abs(x - cx), abs(y - cy)) // 120) + 1))
        for i in range(1, steps + 1):
            self._move_raw(cx + (x - cx) * i // steps, cy + (y - cy) * i // steps)
            time.sleep(0.008)
        self._move_raw(x, y)
        self._pos = (x, y)

    # ---------------------------------------------------------------- mouse

    def click(self, x: int, y: int, button: str = "left") -> None:
        if button not in evdev.BUTTONS:
            raise ValueError(f"unknown button {button!r}")
        self.move_abs(x, y)
        time.sleep(0.04)
        self._button(button, True)
        time.sleep(0.05)
        self._button(button, False)

    def _button(self, button: str, down: bool) -> None:
        self._pointer.emit(evdev.EV_KEY, evdev.BUTTONS[button], 1 if down else 0)
        self._pointer.syn()

    def double_click(self, x: int, y: int, button: str = "left") -> None:
        self.click(x, y, button)
        time.sleep(0.09)
        self.click(x, y, button)

    def right_click(self, x: int, y: int) -> None:
        self.click(x, y, "right")

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
        self.move_abs(x1, y1)
        time.sleep(0.6)
        self._button(button, True)
        time.sleep(0.25)
        self.move_abs(x2, y2, interpolate=False)
        time.sleep(0.25)
        self._button(button, False)

    def scroll(self, dx: int, dy: int) -> None:
        if dy:
            self._pointer.emit(evdev.EV_REL, evdev.REL_WHEEL, int(dy))
            for _ in range(max(1, min(5, abs(int(dy)))) - 1):
                self._pointer.syn()
                time.sleep(0.02)
        if dx:
            self._pointer.emit(evdev.EV_REL, evdev.REL_HWHEEL, int(dx))
        self._pointer.syn()

    # ---------------------------------------------------------------- keyboard

    def type_text(self, text: str) -> None:
        for ch in text:
            kp = evdev.char_to_key(ch)
            if kp is None:
                log.warning("untypable char %r skipped", ch)
                continue
            code, needs_shift = kp
            if needs_shift:
                self._key_down(evdev.KEY_LEFTSHIFT)
            self._key_tap(code)
            if needs_shift:
                self._key_up(evdev.KEY_LEFTSHIFT)
            time.sleep(0.012)

    def _key_down(self, code: int) -> None:
        self._kbd.emit(evdev.EV_KEY, code, 1)
        self._kbd.syn()

    def _key_up(self, code: int) -> None:
        self._kbd.emit(evdev.EV_KEY, code, 0)
        self._kbd.syn()

    def _key_tap(self, code: int, hold: float = 0.02) -> None:
        self._key_down(code)
        time.sleep(hold)
        self._key_up(code)

    def key(self, combo: str) -> None:
        """`ctrl+l`, `super`, `alt+F4`, `Return`, ... (xdotool-ish spelling)."""
        parts = [p.strip() for p in combo.replace("+", " ").split() if p.strip()]
        if not parts:
            return
        mods, main = parts[:-1], parts[-1]
        mod_codes = []
        for m in mods:
            code = evdev.MODIFIERS.get(m.lower())
            if code is None:
                raise ValueError(f"unknown modifier {m!r}")
            mod_codes.append(code)
        code = evdev.key_code(main)
        if code is None:
            raise ValueError(f"unknown key {main!r}")
        for c in mod_codes:
            self._key_down(c)
            time.sleep(0.012)
        self._key_tap(code, hold=0.03)
        for c in reversed(mod_codes):
            self._key_up(c)
            time.sleep(0.012)

    def key_combo(self, combo: str) -> None:  # alias used by the agent loop
        self.key(combo)
