"""Desktop-worker ops for the HUD process (system python: GTK, Atspi, Gst).

The daemon (venv python, no PyGObject) sends requests over hud.sock:

    {"type": "req", "id": 42, "op": "screenshot" | "atspi" | "screen-init"}

and receives replies on ctl.sock:

    {"cmd": "res", "id": 42, "ok": true, "data": {...}}

Ops:
  screen-init   set up the xdg-desktop-portal ScreenCast session (the user
                sees ONE permission dialog; a restore token persists it)
  screenshot    grab a frame from the PipeWire stream -> PNG (base64)
  atspi         accessibility tree of the focused window ->
                [{role, name, x, y, w, h, focused, is_password}]
"""

from __future__ import annotations

import base64
import secrets as _secrets

MAX_ELEMENTS = 220
MAX_DEPTH = 14

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_BASE = "/org/freedesktop/portal/desktop"


def _png_size(data: bytes) -> tuple[int, int]:
    """Parse PNG IHDR (signature 8B, len 4B, 'IHDR' 4B, then width/height)."""
    return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))


class ScreenCast:
    """Continuous screen capture: portal ScreenCast + GStreamer pipewiresrc.

    The FIRST call shows the compositor's permission dialog (once per restore
    token lifetime; persistent when the user picks 'Always allow' or the
    portal supports restore tokens, which GNOME does).
    """

    def __init__(self):
        self._pipeline = None
        self._appsink = None
        self._restore_token = None
        self._ready = False

    # ------------------------------------------------------------- portal

    def init(self) -> dict:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            gi.require_version("GLib", "2.0")
            from gi.repository import Gio, GLib

            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            sender = bus.get_unique_name()[1:].replace(".", "_")
            ctx = GLib.MainContext.new()
            loop = GLib.MainLoop.new(ctx, False)
            results: dict = {}

            def subscribe(token: str, cb):
                req_path = f"{PORTAL_BASE}/request/{sender}/{token}"
                return bus.signal_subscribe(
                    None, "org.freedesktop.portal.Request", "Response",
                    req_path, None, Gio.DBusSignalFlags.NONE, cb)

            def on_response(conn, s, path, iface, signal, params):
                code, res = params.unpack()[:2]
                results.update(res)
                results["_code"] = code
                loop.quit()

            GLib.MainContext.push_thread_default(ctx)
            try:
                # ---- 1. CreateSession
                token = "alpha" + _secrets.token_hex(6)
                subscribe(token, on_response)
                bus.call_sync(PORTAL, PORTAL_BASE, "org.freedesktop.portal.ScreenCast",
                              "CreateSession",
                              GLib.Variant("(a{sv})", ({"handle_token": GLib.Variant("s", token),
                                                         "session_handle_token": GLib.Variant("s", "s" + token)},)),
                              None, Gio.DBusCallFlags.NONE, -1, None)
                loop.run()
                session = results.get("session_handle")
                if not session:
                    return {"ok": False, "error": "CreateSession failed (no session_handle)"}

                # ---- 2. SelectSources (persist permission via restore token)
                sel_opts = {
                    "handle_token": GLib.Variant("s", "sel" + token),
                    "types": GLib.Variant("u", 1),          # MONITOR
                    "multiple": GLib.Variant("b", False),
                    "cursor_mode": GLib.Variant("u", 1),    # HIDDEN
                    "persist_mode": GLib.Variant("u", 2),   # PERSISTENT
                }
                if self._restore_token:
                    sel_opts["restore_token"] = GLib.Variant("s", self._restore_token)
                subscribe("sel" + token, on_response)
                bus.call_sync(PORTAL, PORTAL_BASE, "org.freedesktop.portal.ScreenCast",
                              "SelectSources",
                              GLib.Variant("(oa{sv})", (session, sel_opts)),
                              None, Gio.DBusCallFlags.NONE, -1, None)
                loop.run()
                if results.get("_code") != 0:
                    return {"ok": False, "error": f"SelectSources denied (code {results.get('_code')})"}
                if results.get("restore_token"):
                    self._restore_token = results["restore_token"]

                # ---- 3. Start — the permission dialog happens here
                start_opts = {"handle_token": GLib.Variant("s", "st" + token)}
                subscribe("st" + token, on_response)
                bus.call_sync(PORTAL, PORTAL_BASE, "org.freedesktop.portal.ScreenCast",
                              "Start",
                              GLib.Variant("(osa{sv})", (session, "", start_opts)),
                              None, Gio.DBusCallFlags.NONE, -1, None)
                loop.run()
                if results.get("_code") != 0:
                    return {"ok": False, "error": f"Start denied/cancelled (code {results.get('_code')})"}
                streams = results.get("streams") or []
                if not streams:
                    return {"ok": False, "error": "no streams"}
                if results.get("restore_token"):
                    self._restore_token = results["restore_token"]
                node_id = streams[0]["id"]

                # ---- 4. OpenPipeWireRemote (returns a UNIX fd via fd-list)
                res, fdlist = bus.call_with_unix_fd_list_sync(
                    PORTAL, PORTAL_BASE, "org.freedesktop.portal.ScreenCast",
                    "OpenPipeWireRemote",
                    GLib.Variant("(oa{sv})", (session,
                        {"handle_token": GLib.Variant("s", "fd" + token)})),
                    GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
                fd = fdlist.get(res.unpack()[0])
            finally:
                GLib.MainContext.pop_thread_default(ctx)

            # ---- 5. GStreamer: pipewire -> pngenc -> appsink
            self._build_pipeline(fd, node_id)
            self._ready = True
            return {"ok": True, "persisted": bool(self._restore_token)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _build_pipeline(self, fd: int, node_id: int) -> None:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        from gi.repository import Gst, GstApp

        Gst.init(None)
        self._fd = fd  # keep the OS fd open for the lifetime of the pipeline
        self._pipeline = Gst.parse_launch(
            f"pipewiresrc fd={fd} path={node_id} keepalive-time=1000 "
            f"always-copy=true ! videoconvert ! pngenc ! "
            f"appsink name=sink max-buffers=2 drop=true"
        )
        self._appsink = self._pipeline.get_by_name("sink")
        self._pipeline.set_state(Gst.State.PLAYING)

    # ------------------------------------------------------------- frames

    def screenshot(self) -> dict:
        if not self._ready:
            r = self.init()
            if not r.get("ok"):
                return r
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            sample = None
            for _ in range(60):  # up to ~3s for first/current frame
                sample = self._appsink.try_pull_sample(0.05)
                if sample:
                    break
            if sample is None:
                return {"ok": False, "error": "no frame from stream"}
            buf = sample.get_buffer()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok:
                return {"ok": False, "error": "buffer map failed"}
            data = bytes(mapinfo.data)
            buf.unmap(mapinfo)
            w, h = _png_size(data)
            return {"ok": True, "width": w, "height": h,
                    "png_b64": base64.b64encode(data).decode()}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# AT-SPI tree of the focused window
# ---------------------------------------------------------------------------


def atspi_focused_tree() -> dict:
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        desktop = Atspi.get_desktop(0)
        focused_win = None
        focused_app = None
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            for j in range(app.get_child_count()):
                win = app.get_child_at_index(j)
                if win is None:
                    continue
                try:
                    if win.get_state_set().contains(Atspi.StateType.ACTIVE):
                        focused_win, focused_app = win, app
                        break
                except Exception:
                    continue
            if focused_win:
                break
        if focused_win is None:
            return {"ok": False, "error": "no focused window"}

        elements: list[dict] = []

        def walk(node, depth):
            if len(elements) >= MAX_ELEMENTS or depth > MAX_DEPTH:
                return
            try:
                role = node.get_role_name() or ""
                name = (node.get_name() or "")[:80]
            except Exception:
                return
            try:
                ext = node.get_extents(Atspi.CoordType.SCREEN)
                if ext and (ext.width > 1 or ext.height > 1):
                    try:
                        states = node.get_state_set()
                        focused = states.contains(Atspi.StateType.FOCUSED)
                        showing = states.contains(Atspi.StateType.SHOWING)
                    except Exception:
                        focused = showing = False
                    is_password = "password" in role.lower()
                    if showing or focused or role in (
                            "application", "window", "frame", "dialog", "page tab list"):
                        elements.append({
                            "role": role, "name": name,
                            "x": ext.x, "y": ext.y, "w": ext.width, "h": ext.height,
                            "focused": bool(focused), "is_password": bool(is_password),
                            "depth": depth,
                        })
            except Exception:
                pass
            try:
                for k in range(node.get_child_count()):
                    walk(node.get_child_at_index(k), depth + 1)
            except Exception:
                return

        walk(focused_win, 0)
        return {
            "ok": True,
            "app": focused_app.get_name() if focused_app else "",
            "window": focused_win.get_name() or "",
            "elements": elements,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
