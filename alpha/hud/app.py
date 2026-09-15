#!/usr/bin/env python3
"""Alpha HUD — floating overlay window (M3).

Runs as a STANDALONE process with the SYSTEM python (which has PyGObject +
GTK4): the daemon spawns `python3 alpha/hud/app.py <socket>` and feeds it JSON
lines over a unix socket:

    {"type": "state", "state": "listening", "text": "..."}
    {"type": "mute", "muted": true}

Always-on-top strategy (best available wins):
  1. gtk4-layer-shell        (wlroots compositors; NOT supported by GNOME)
  2. X11 keep-above          (GNOME on Xorg — the spec's recommended session)
  3. plain window            (GNOME Wayland fallback; may not cover fullscreen)

Also exposes a minimal StatusNotifierItem (tray icon) for desktops that host
one (KDE/XFCE/AppIndicator extension); stock GNOME has no tray by default.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from worker import ScreenCast, atspi_focused_tree  # noqa: E402

SOCK_PATH = os.environ.get("ALPHA_HUD_SOCK", os.path.expanduser("~/.local/state/alpha/hud.sock"))
CTL_PATH = os.environ.get("ALPHA_CTL_SOCK", os.path.expanduser("~/.local/state/alpha/ctl.sock"))

STATE_COLORS = {
    "idle": "#3c414b",
    "listening": "#3584e4",   # blue — mic is recording
    "thinking": "#f5c211",    # amber
    "acting": "#ff7800",      # orange
    "speaking": "#33d17a",    # green
    "error": "#e01b24",       # red
    "muted": "#77767b",       # gray
}

CSS = """
window {
    background-color: rgba(20, 22, 28, 0.92);
    border-radius: 14px;
}
.box {
    padding: 10px 16px;
}
.title {
    font-weight: bold;
    font-size: 12px;
    color: #ffffff;
    opacity: 0.75;
}
.statebar {
    border-radius: 4px;
    min-height: 6px;
}
.text {
    font-size: 14px;
    color: #ffffff;
}
.mutebtn {
    font-size: 12px;
    color: #ffffff;
    background: rgba(255,255,255,0.12);
    border-radius: 8px;
    padding: 2px 10px;
}
"""


class HUD:
    def __init__(self) -> None:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk, Gio, GLib, Gtk

        self.gtk = Gtk
        self.glib = GLib
        self.muted = False
        self.state = "idle"
        self._sni = None
        self._screencast = ScreenCast()

        self.app = Gtk.Application(application_id="org.alphaagent.Hud")
        self.app.connect("activate", self._activate)

    # ------------------------------------------------------------------ UI
    def _activate(self, app) -> None:
        Gtk = self.gtk

        self.win = Gtk.ApplicationWindow(application=app)
        self.win.set_default_size(420, 90)
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_opacity(0.97)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            self.win.get_display(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["box"])
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.title = Gtk.Label(label="ALPHA", css_classes=["title"], halign=Gtk.Align.START)
        self.mute_btn = Gtk.Button(label="Mute", css_classes=["mutebtn"])
        self.mute_btn.connect("clicked", lambda *_: self._ctl("mute-toggle"))
        head.append(self.title)
        head.append(Gtk.Label(hexpand=True))  # spacer
        head.append(self.mute_btn)

        self.statebar = Gtk.Box(css_classes=["statebar"])
        self.statebar.set_size_request(-1, 6)

        self.text = Gtk.Label(
            label="…", css_classes=["text"], wrap=True,
            halign=Gtk.Align.START, xalign=0,
        )

        outer.append(head)
        outer.append(self.statebar)
        outer.append(self.text)
        self.win.set_child(outer)

        self._pin_on_top()

        # GTK4 has no move() — Wayland forbids client-side positioning, so the
        # compositor places the window (typically centered, which is fine).

        self.win.present()
        self._report_geometry()
        self._start_sni()
        self._connect_socket()

    def _report_geometry(self) -> None:
        """Send the real monitor layout to the daemon (input/vision coords)."""
        try:
            mons = []
            disp = self.win.get_display()
            n = disp.get_monitors().get_n_items()
            for i in range(n):
                m = disp.get_monitors().get_item(i)
                geo = m.get_geometry()
                scale = m.get_scale_factor()
                mons.append({
                    "name": m.get_model() or f"mon{i}",
                    "x": geo.x, "y": geo.y, "w": geo.width, "h": geo.height,
                    "scale": scale,
                })
            self._ctl_raw({"cmd": "set-geom", "monitors": mons})
            print("hud: geometry reported:", mons, flush=True)
        except Exception as e:
            print("hud: geometry report failed:", e, flush=True)

    def _pin_on_top(self) -> None:
        """Best-effort always-on-top (see module docstring)."""
        # 1. layer-shell (wlroots)
        try:
            import gi

            gi.require_version("GtkLayerShell", "0.1")
            from gi.repository import GtkLayerShell

            GtkLayerShell.init_for_window(self.win)
            GtkLayerShell.set_layer(self.win, GtkLayerShell.Layer.TOP)
            GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.TOP, True)
            print("hud: pinned via gtk4-layer-shell", flush=True)
            return
        except Exception:
            pass
        # 2. X11 keep-above
        try:
            if self.win.get_display().get_type().name == "GdkX11Display":
                self.win.set_keep_above(True)
                print("hud: pinned via X11 keep-above", flush=True)
                return
        except Exception:
            pass
        print(
            "hud: WARNING — no always-on-top on this compositor "
            "(GNOME Wayland); window may be covered by fullscreen apps. "
            "Use 'GNOME on Xorg' for the guaranteed path.",
            flush=True,
        )

    def _place_top_center(self) -> None:
        # GTK4/Wayland: clients cannot position their own windows. Kept for
        # X11 where set_position existed in GTK3; in GTK4 even X11 uses the
        # compositor. No-op.
        pass

    # ------------------------------------------------------------- updates
    def _apply(self, state: str, text: str, muted: bool) -> None:
        self.state = state if state in STATE_COLORS else "idle"
        color = STATE_COLORS["muted"] if muted else STATE_COLORS[self.state]
        self.statebar.set_css_classes(["statebar"])
        # set background via inline style
        self.statebar.get_style_context().add_provider(
            self._bg_provider(color),
            self.gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self.text.set_text(text or "…")
        self.title.set_text("ALPHA" + (" — MUTED" if muted else ""))
        self.mute_btn.set_label("Unmute" if muted else "Mute")
        if state in ("listening", "thinking", "acting", "speaking", "error") and not muted:
            self.win.present()

    def _bg_provider(self, color: str):
        Gtk = self.gtk
        p = Gtk.CssProvider()
        p.load_from_data(f".statebar {{ background-color: {color}; }}".encode())
        return p

    # ------------------------------------------------------------- IPC
    def _connect_socket(self) -> None:
        t = threading.Thread(target=self._sock_loop, daemon=True)
        t.start()

    def _sock_loop(self) -> None:
        GLib = self.glib
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(SOCK_PATH)
                buf = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        GLib.idle_add(self._on_msg, msg)
            except OSError:
                pass  # daemon restarting; retry
            import time

            time.sleep(1.0)

    def _on_msg(self, msg: dict) -> bool:
        if msg.get("type") == "state":
            self._apply(msg.get("state", "idle"), msg.get("text", ""), self.muted)
        elif msg.get("type") == "mute":
            self.muted = bool(msg.get("muted"))
            self._apply(self.state, "", self.muted)
        elif msg.get("type") == "req":
            self._handle_request(msg)
        return False

    def _handle_request(self, msg: dict) -> None:
        """Run a worker op (possibly slow) in a thread, reply via ctl socket."""
        op = msg.get("op")
        rid = msg.get("id")

        def run():
            if op == "screen-init":
                data = self._screencast.init()
            elif op == "screenshot":
                data = self._screencast.screenshot()
            elif op == "atspi":
                data = atspi_focused_tree()
            else:
                data = {"ok": False, "error": f"unknown op {op!r}"}
            self._ctl_raw({"cmd": "res", "id": rid, **data})

        threading.Thread(target=run, daemon=True).start()

    def _ctl(self, cmd: str) -> None:
        self._ctl_raw({"cmd": cmd})

    def _ctl_raw(self, msg: dict) -> None:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(CTL_PATH)
            s.sendall(json.dumps(msg).encode() + b"\n")
            s.close()
        except OSError as e:
            print("hud: ctl send failed:", e, flush=True)

    # ------------------------------------------------------------- tray
    def _start_sni(self) -> None:
        """Minimal StatusNotifierItem (tray) for desktops that host one."""
        try:
            from gi.repository import Gio, GLib

            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._sni = conn  # keep alive
            node_xml = """
            <node>
              <interface name='org.kde.StatusNotifierItem'>
                <property name='Category' type='s' access='read'/>
                <property name='Id' type='s' access='read'/>
                <property name='Title' type='s' access='read'/>
                <property name='Status' type='s' access='read'/>
                <property name='IconName' type='s' access='read'/>
                <property name='ToolTip' type='(sa(iiay)ss)' access='read'/>
                <method name='Activate'>
                  <arg type='i' direction='in'/><arg type='i' direction='in'/>
                </method>
                <method name='SecondaryActivate'>
                  <arg type='i' direction='in'/><arg type='i' direction='in'/>
                </method>
              </interface>
            </node>"""
            node = Gio.DBusNodeInfo.new_for_xml(node_xml)
            iface = node.interfaces[0]

            def get_prop(conn, sender, path, iface_name, prop):
                if prop == "Category":
                    return GLib.Variant("s", "ApplicationStatus")
                if prop == "Id":
                    return GLib.Variant("s", "alpha-agent")
                if prop == "Title":
                    return GLib.Variant("s", "Alpha")
                if prop == "Status":
                    return GLib.Variant("s", "Active" if not self.muted else "Passive")
                if prop == "IconName":
                    return GLib.Variant("s", "audio-input-microphone")
                if prop == "ToolTip":
                    return GLib.Variant("(sa(iiay)ss)", ("", [], "", "Alpha voice assistant"))
                return None

            def call(conn, sender, path, name, method, params, invocation):
                if method.get_name() in ("Activate", "SecondaryActivate"):
                    self._ctl("mute-toggle")
                invocation.return_value(None)

            conn.register_object(
                "/StatusNotifierItem", iface,
                call, get_prop, None,
            )
            # register with a StatusNotifierWatcher if one exists
            try:
                conn.call_sync(
                    "org.kde.StatusNotifierWatcher",
                    "/StatusNotifierWatcher",
                    "org.kde.StatusNotifierWatcher",
                    "RegisterStatusNotifierItem",
                    GLib.Variant("(s)", ("/StatusNotifierItem",)),
                    None, Gio.DBusCallFlags.NONE, -1, None,
                )
                print("hud: tray icon registered", flush=True)
            except GLib.Error as e:
                print("hud: no StatusNotifierWatcher (GNOME has no tray by default):",
                      str(e)[:80], flush=True)
        except Exception as e:
            print("hud: SNI setup skipped:", str(e)[:80], flush=True)


def main() -> int:
    hud = HUD()
    return hud.app.run(None)


if __name__ == "__main__":
    sys.exit(main())
