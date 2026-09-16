"""Linux uinput/evdev constants and key-name mapping (stable kernel ABI).

Avoids the python-evdev dependency (which has no maintained cp311 wheels on
Fedora) — these values come from include/uapi/linux/input-event-codes.h and
include/uapi/linux/uinput.h, which are frozen ABI.
"""

from __future__ import annotations

import struct

# event types
EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03

SYN_REPORT = 0x00

# axes / relative
ABS_X = 0x00
ABS_Y = 0x01
REL_X = 0x00
REL_Y = 0x01
REL_WHEEL = 0x08
REL_HWHEEL = 0x06

# mouse buttons
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112

# uinput ioctls (_IOW('U', nr, int) / _IO('U', nr) — computed for 64-bit)


def _IOC(dir_: int, type_: int, nr: int, size: int) -> int:
    return (dir_ << 30) | (size << 16) | (type_ << 8) | nr


_IOC_WRITE = 1
_IOC_NONE = 0
_IOC_READ = 2

UI_SET_EVBIT = _IOC(_IOC_WRITE, ord("U"), 100, 4)
UI_SET_KEYBIT = _IOC(_IOC_WRITE, ord("U"), 101, 4)
UI_SET_RELBIT = _IOC(_IOC_WRITE, ord("U"), 102, 4)
UI_SET_ABSBIT = _IOC(_IOC_WRITE, ord("U"), 103, 4)
UI_SET_PROPBIT = _IOC(_IOC_WRITE, ord("U"), 110, 4)
UI_DEV_CREATE = _IOC(_IOC_NONE, ord("U"), 1, 0)
UI_DEV_DESTROY = _IOC(_IOC_NONE, ord("U"), 2, 0)

# input properties (include/uapi/linux/input.h)
INPUT_PROP_POINTER = 0x00
INPUT_PROP_DIRECT = 0x01

ABS_CNT = 64

# uinput_user_dev struct
_UINPUT_USER_DEV = struct.Struct("80s 4H i 64i 64i 64i 64i")


def uinput_user_dev(name: bytes, bustype: int, vendor: int, product: int,
                    version: int, absmax: dict[int, int]) -> bytes:
    maxs = [absmax.get(i, 0) for i in range(ABS_CNT)]
    mins = [0] * ABS_CNT
    fuzz = [0] * ABS_CNT
    flat = [0] * ABS_CNT
    return _UINPUT_USER_DEV.pack(
        name.ljust(80, b"\0")[:80], bustype, vendor, product, version, 0,
        *maxs, *mins, *fuzz, *flat,
    )


def input_event(time_s: int, time_us: int, type_: int, code: int, value: int) -> bytes:
    # struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
    return struct.pack("llhhi", time_s, time_us, type_, code, value)


# ---------------------------------------------------------------------------
# Key codes: name -> evdev code. xdotool-style names map onto these.
# ---------------------------------------------------------------------------

KEY_LEFTCTRL = 29
KEY_LEFTSHIFT = 42
KEY_LEFTALT = 56
KEY_LEFTMETA = 125
KEY_RIGHTCTRL = 97
KEY_RIGHTSHIFT = 54
KEY_RIGHTALT = 100
KEY_RIGHTMETA = 126

MODIFIERS = {
    "ctrl": KEY_LEFTCTRL, "control": KEY_LEFTCTRL,
    "shift": KEY_LEFTSHIFT,
    "alt": KEY_LEFTALT,
    "super": KEY_LEFTMETA, "meta": KEY_LEFTMETA, "win": KEY_LEFTMETA,
}

KEYMAP: dict[str, int] = {
    **MODIFIERS,
    # letters (evdev order)
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34, "h": 35,
    "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49, "o": 24, "p": 25,
    "q": 16, "r": 19, "s": 31, "t": 20, "u": 22, "v": 47, "w": 17, "x": 45,
    "y": 21, "z": 44,
    # digits
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    # navigation / editing
    "Return": 28, "Enter": 28, "enter": 28, "return": 28,
    "space": 57, "Space": 57,
    "Tab": 15, "tab": 15,
    "BackSpace": 14, "backspace": 14, "Backspace": 14,
    "Escape": 1, "Esc": 1, "escape": 1,
    "Delete": 111, "delete": 111, "DEL": 111,
    "Home": 102, "End": 107,
    "Page_Up": 104, "Page_Down": 109, "Prior": 104, "Next": 109,
    "Up": 103, "Down": 108, "Left": 105, "Right": 106,
    "Insert": 110,
    # function keys
    **{f"F{i}": 58 + i for i in range(1, 13)},
    # punctuation
    "-": 12, "_": 12, "=": 13, "+": 13,
    "[": 26, "]": 27, "{": 26, "}": 27,
    ";": 39, ":": 39, "'": 40, '"': 40,
    "`": 41, "~": 41, "\\": 43, "|": 43,
    ",": 51, "<": 51, ".": 52, ">": 52, "/": 53, "?": 53,
    "Caps_Lock": 58, "capslock": 58,
    "Print": 99, "print": 99,
}

# chars that require shift (US layout)
_SHIFT_CHARS = set("!@#$%^&*()_+{}|:\"<>?~")

BUTTONS = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}


# Case-insensitive aliases for key NAMES (as opposed to characters).
_KEY_ALIASES: dict[str, str] = {
    "esc": "Escape", "escape": "Escape",
    "ret": "Return", "cr": "Return", "kp_enter": "Return",
    "pgup": "Page_Up", "pgdn": "Page_Down",
    "pageup": "Page_Up", "pagedown": "Page_Down",
    "del": "Delete", "bsp": "BackSpace", "bksp": "BackSpace",
    "spc": "space", "tab": "Tab",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "insert": "Insert",
    "enter": "Return", "return": "Return", "backspace": "BackSpace",
    "capslock": "Caps_Lock", "caps_lock": "Caps_Lock",
}


def key_code(name: str) -> int | None:
    """Resolve a key NAME (`Return`, `F4`, `super`, `ctrl`, `space`) to a code."""
    if name in KEYMAP:
        return KEYMAP[name]
    lowered = name.lower()
    if lowered in _KEY_ALIASES:
        return KEYMAP[_KEY_ALIASES[lowered]]
    for candidate in (lowered, name.upper(), name.capitalize()):
        if candidate in KEYMAP:
            return KEYMAP[candidate]
    return None


def char_to_key(ch: str) -> tuple[int, bool] | None:
    """Return (keycode, needs_shift) for a printable char, or None.

    Only ASCII/US-layout characters are supported; anything else returns None
    so the caller can skip it (a non-ASCII char must not raise).
    """
    if ch == " ":
        return KEYMAP["space"], False
    if ch.isalpha():
        if ch.lower() not in KEYMAP:
            return None
        return KEYMAP[ch.lower()], ch.isupper()
    if ch.isdigit():
        return KEYMAP[ch], False
    if ch in KEYMAP:
        return KEYMAP[ch], ch in _SHIFT_CHARS
    shift_digit = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
                   "^": "6", "&": "7", "*": "8", "(": "9", ")": "0"}
    if ch in shift_digit:
        return KEYMAP[shift_digit[ch]], True
    return None
