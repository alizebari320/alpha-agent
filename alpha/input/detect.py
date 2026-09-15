"""Pick the input backend for this session (§5).

Priority:
  - native uinput backend when /dev/uinput is accessible (works on BOTH
    Wayland and X11 — it sits below the display server)
  - X11 backend via xdotool when running under Xorg and uinput is unavailable
"""

from __future__ import annotations

import logging
import os

from .base import InputBackend

log = logging.getLogger(__name__)


def detect_backend(screen_w: int | None = None, screen_h: int | None = None) -> InputBackend:
    session = os.environ.get("XDG_SESSION_TYPE", "x11")

    # Native uinput works everywhere if we have permission
    if os.access("/dev/uinput", os.W_OK):
        if screen_w and screen_h:
            from .uinput import UInputBackend

            return UInputBackend(screen_w, screen_h)
        log.warning("uinput available but screen size unknown; falling back")

    if session == "x11":
        from .x11 import X11Backend

        return X11Backend()

    raise RuntimeError(
        "no input backend available: /dev/uinput not writable "
        "(add user to 'input' group) and session is Wayland (xdotool won't work)"
    )
