"""Vision + grounding (§6) — daemon side.

The HUD worker (system python) does the compositor-priv things: portal
ScreenCast frames and AT-SPI queries. This module orchestrates and does the
pure-python parts: downscale, Set-of-Mark annotation, coordinate scaling, and
the password-field lock.

COORDINATE MATH (the #1 failure mode of these projects — unit-tested):
  Model coordinates are in DOWNSCALED screenshot space (max_width 1280).
  To inject:  screen = model * scale + monitor_origin
  To label:   model = (screen - origin) / scale
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from dataclasses import dataclass

from ..ipc import CTL_SOCK, HUD_SOCK

log = logging.getLogger(__name__)

_req_counter = 0


class PasswordFieldLocked(Exception):
    """Raised when the focused element is a password field (§10)."""


@dataclass
class Screenshot:
    png_b64: str
    width: int
    height: int
    monitor: dict  # {name,x,y,w,h,scale} of the ACTIVE monitor


@dataclass
class AtspiElement:
    role: str
    name: str
    x: int
    y: int
    w: int
    h: int
    focused: bool
    is_password: bool
    depth: int


class DesktopWorker:
    """Request/response with the HUD process over the existing sockets."""

    def __init__(self, hud_sock=HUD_SOCK, ctl_sock=CTL_SOCK):
        self.hud_sock = hud_sock
        self.ctl_sock = ctl_sock
        self._pending: dict[int, asyncio.Future] = {}
        self._writer = None

    def attach(self, daemon) -> None:
        """Hook into the daemon's servers so 'res' replies resolve futures."""
        self._daemon = daemon
        daemon._vision_worker = self

    async def _send(self, op: str, args: dict | None = None, timeout: float = 12.0) -> dict:
        global _req_counter
        _req_counter += 1
        rid = _req_counter
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg = {"type": "req", "id": rid, "op": op, **(args or {})}
        # hud.sock LineServer pushes to HUD; reuse the daemon's broadcast
        self._daemon.hud.broadcast(msg)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    def resolve(self, msg: dict) -> None:
        """Called by the daemon when a 'res' arrives on ctl.sock."""
        rid = msg.get("id")
        fut = self._pending.get(rid)
        if fut and not fut.done():
            fut.set_result({k: v for k, v in msg.items() if k not in ("cmd", "id")})


# ---------------------------------------------------------------------------
# Coordinate scaling (pure functions — unit tested)
# ---------------------------------------------------------------------------


def downscale_size(w: int, h: int, max_width: int = 1280) -> tuple[int, int]:
    if w <= max_width:
        return w, h
    ratio = max_width / w
    return max_width, max(1, round(h * ratio))


def model_to_screen(mx: float, my: float, shot_w: int, shot_h: int,
                    monitor: dict, max_width: int = 1280) -> tuple[int, int]:
    """Downscaled-model coords -> absolute screen coords."""
    real_w, real_h = monitor["w"], monitor["h"]
    sx = mx * (real_w / shot_w)
    sy = my * (real_h / shot_h)
    return round(monitor["x"] + sx), round(monitor["y"] + sy)


def screen_to_model(sx: float, sy: float, shot_w: int, shot_h: int,
                    monitor: dict) -> tuple[float, float]:
    """Absolute screen coords -> downscaled-model coords."""
    real_w, real_h = monitor["w"], monitor["h"]
    return ((sx - monitor["x"]) * shot_w / real_w,
            (sy - monitor["y"]) * shot_h / real_h)


def clip_to_monitor(x: float, y: float, monitor: dict) -> tuple[float, float]:
    return (
        max(monitor["x"], min(x, monitor["x"] + monitor["w"] - 1)),
        max(monitor["y"], min(y, monitor["y"] + monitor["h"] - 1)),
    )


# ---------------------------------------------------------------------------
# SoM — Set-of-Mark annotation
# ---------------------------------------------------------------------------


def som_label(elements: list[AtspiElement], shot_w: int, shot_h: int, monitor: dict,
              max_elements: int = 24) -> tuple[list[dict], dict[int, dict]]:
    """Pick clickable candidates, map them to model space, assign numbered labels.

    Returns (labels_for_prompt, label_map) where label_map[i] gives the
    CENTER in model coords for click verification.
    """
    clickable = {
        "push button", "toggle button", "button", "link", "menu item", "check box",
        "radio button", "combo box", "text", "entry", "tab", "list item",
        "menu", "tree item", "spin button", "page tab", "hyperlink",
    }
    cands = []
    for el in elements:
        if el.is_password:
            continue  # never offer password fields to the model
        if el.role.lower() in clickable or "button" in el.role.lower() or el.role.lower() == "link":
            cands.append(el)
    # keep the biggest/most visible candidates first
    cands.sort(key=lambda e: e.w * e.h, reverse=True)
    cands = cands[:max_elements]

    labels = []
    label_map = {}
    for i, el in enumerate(cands, start=1):
        cx = el.x + el.w / 2
        cy = el.y + el.h / 2
        mx, my = screen_to_model(cx, cy, shot_w, shot_h, monitor)
        desc = f"{el.role}: {el.name}" if el.name else el.role
        labels.append({"label": str(i), "desc": desc[:60]})
        label_map[str(i)] = {"x": round(mx, 1), "y": round(my, 1),
                             "screen_x": int(cx), "screen_y": int(cy)}
    return labels, label_map


def som_table_text(labels: list[dict]) -> str:
    return "\n".join(f"{lbl['label']}: {lbl['desc']}" for lbl in labels)


def draw_som_png(png_b64: str, label_map: dict[int, dict]) -> str:
    """Draw numbered boxes on the (already downscaled) screenshot.

    Returns base64 PNG. Uses Pillow if available; falls back to the plain
    screenshot (label table still works text-only).
    """
    try:
        from PIL import Image, ImageDraw

        img = Image.open(io.BytesIO(base64.b64decode(png_b64)))
        draw = ImageDraw.Draw(img)
        for label, pos in label_map.items():
            x, y = int(pos["x"]), int(pos["y"])
            # box around the label point (element centers; 24px box)
            draw.rectangle([x - 14, y - 14, x + 14, y + 14], outline="#ff2e88", width=2)
            draw.text((x - 10, y - 10), str(label), fill="#ff2e88")
        out = io.BytesIO()
        img.save(out, "PNG")
        return base64.b64encode(out.getvalue()).decode()
    except Exception as e:
        log.warning("SoM drawing unavailable (%s); returning plain shot", e)
        return png_b64


# ---------------------------------------------------------------------------
# High-level capture API
# ---------------------------------------------------------------------------


class Vision:
    """Facade used by the agent loop (M6)."""

    def __init__(self, worker: DesktopWorker, daemon, password_lock: bool = True,
                 max_width: int = 1280):
        self.worker = worker
        self.daemon = daemon
        self.password_lock = password_lock
        self.max_width = max_width
        self._share_warned = False

    def _active_monitor(self) -> dict:
        mons = self.daemon.monitors or [{"name": "primary", "x": 0, "y": 0,
                                         "w": 1920, "h": 1080, "scale": 1}]
        # single monitor: use it; multi-monitor: pick by focused window later (M7)
        return mons[0]

    async def screenshot(self, downscale: bool = True) -> Screenshot | None:
        try:
            data = await self.worker._send("screenshot", timeout=15.0)
        except TimeoutError:
            # The HUD is alive but could not produce a frame — almost always
            # because the portal screen-sharing prompt was never approved.
            if not self._share_warned:
                log.warning("screenshot unavailable — grant screen sharing in the "
                            "Alpha HUD (Share button); continuing without vision")
                self._share_warned = True
            return None
        if not data.get("ok"):
            log.warning("screenshot failed: %s", data.get("error"))
            return None
        png_b64 = data["png_b64"]
        w, h = data["width"], data["height"]
        mon = self._active_monitor()
        if downscale and w > self.max_width:
            try:
                from PIL import Image

                img = Image.open(io.BytesIO(base64.b64decode(png_b64)))
                nw, nh = downscale_size(w, h, self.max_width)
                img = img.resize((nw, nh))
                buf = io.BytesIO()
                img.save(buf, "PNG")
                png_b64 = base64.b64encode(buf.getvalue()).decode()
                w, h = nw, nh
            except Exception as e:
                log.warning("downscale failed (%s); sending full image", e)
        return Screenshot(png_b64=png_b64, width=w, height=h, monitor=mon)

    async def atspi(self) -> list[AtspiElement]:
        try:
            data = await self.worker._send("atspi", timeout=8.0)
        except TimeoutError:
            log.warning("atspi timed out (accessibility bus busy?)")
            return []
        if not data.get("ok"):
            log.warning("atspi failed: %s", data.get("error"))
            return []
        return [AtspiElement(**e) for e in data.get("elements", [])]

    async def check_password_lock(self, elements: list[AtspiElement] | None = None) -> bool:
        """True when a password field is FOCUSED — refuse to act (§10)."""
        if not self.password_lock:
            return False
        if elements is None:
            elements = await self.atspi()
        for el in elements:
            if el.is_password and el.focused:
                log.warning("password field focused — vision/typing locked "
                            "(role=%s name=%r app=%r)", el.role, el.name, getattr(el, "app", ""))
                return True
        return False
