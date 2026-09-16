#!/usr/bin/env python3
"""Live input-backend self-test — verifies events actually LAND on screen.

Judging input injection from `libinput debug-events` is not enough: events can
reach the kernel and still be ignored by the compositor. This test therefore
uses a *verifiable target*: a fullscreen GTK4 probe (run under the system
python, which has PyGObject) that prints every pointer coordinate, click, and
typed character it receives. We then drive the configured backend and compare
what the probe reported with what we asked for.

Usage:
    python3 scripts/test-input.py            # via the venv
    uv run python scripts/test-input.py

Exit code 0 = all checks passed. Nothing here needs a keyring or the LLM.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PROBE = r'''
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

def on_activate(app):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    entry = Gtk.Entry()
    box.append(Gtk.Label(label="alpha input probe"))
    box.append(entry)
    win = Gtk.ApplicationWindow(application=app)
    win.set_child(box)
    win.fullscreen()
    motion = Gtk.EventControllerMotion()
    motion.connect("motion", lambda c, x, y: print(f"MOTION {x:.0f} {y:.0f}", flush=True))
    win.add_controller(motion)
    click = Gtk.GestureClick()
    click.connect("pressed", lambda g, n, x, y: print(f"CLICK {x:.0f} {y:.0f}", flush=True))
    win.add_controller(click)
    entry.connect("changed", lambda e: print(f"TEXT {e.get_text()}", flush=True))
    win.present()
    entry.grab_focus()

app = Gtk.Application(application_id="dev.alpha.InputProbe")
app.connect("activate", on_activate)
GLib.timeout_add_seconds(45, lambda: (app.quit(), False)[1])
app.run([])
'''


def main() -> int:
    from alpha.input.detect import detect_backend

    screen_w, screen_h = _screen_size()
    print(f"screen: {screen_w}x{screen_h}  session: {os.environ.get('XDG_SESSION_TYPE')}")

    probe = subprocess.Popen(
        ["/usr/bin/python3", "-c", PROBE],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )
    events = _EventFeed(probe)
    print("probe window starting (fullscreen, 45s budget)…")
    time.sleep(4)

    backend = detect_backend(screen_w, screen_h)
    print(f"backend: {type(backend).__name__}\n")

    checks: list[tuple[str, bool, str]] = []
    targets = [(300, 200), (1500, 900), (960, 540)]
    for x, y in targets:
        events.mark()
        backend.move_abs(x, y)
        got = events.last("MOTION", timeout=3)
        ok = got is not None and abs(got[0] - x) <= 1 and abs(got[1] - y) <= 1
        checks.append((f"move_abs({x},{y})", ok, f"probe saw {got}"))

    events.mark()
    backend.click(700, 450)
    got = events.last("CLICK", timeout=3)
    ok = got is not None and abs(got[0] - 700) <= 1 and abs(got[1] - 450) <= 1
    checks.append(("click(700,450)", ok, f"probe saw {got}"))

    events.mark()
    backend.drag(400, 300, 1000, 800)
    got = events.last("MOTION", timeout=3)
    ok = got is not None and abs(got[0] - 1000) <= 2 and abs(got[1] - 800) <= 2
    checks.append(("drag(400,300 -> 1000,800)", ok, f"probe saw {got}"))

    events.mark()
    backend.type_text("alpha ok")
    text = events.text(timeout=3)
    checks.append(("type_text('alpha ok')", text == "alpha ok", f"probe saw {text!r}"))

    backend.key("ctrl+l")  # must not raise on a Wayland session
    checks.append(("key('ctrl+l')", True, "no exception"))

    backend.close()
    probe.terminate()

    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:34s} {detail}")
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


class _EventFeed:
    """Reads the probe's stdout on a thread so no event is ever missed.

    (A select() loop over a buffered TextIOWrapper is unreliable: once Python
    has pulled bytes into its own buffer, select() on the fd reports not-ready
    while lines are still waiting to be read.)
    """

    def __init__(self, probe: subprocess.Popen):
        import threading

        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._stop = False

        def pump() -> None:
            assert probe.stdout is not None  # noqa: S101 - probe script
            for line in probe.stdout:
                with self._lock:
                    self._lines.append(line.strip())
                if self._stop:
                    break

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()

    def mark(self) -> None:
        with self._lock:
            self._lines.clear()

    def _matching(self, kind: str) -> list[str]:
        with self._lock:
            return [ln for ln in self._lines if ln.startswith(kind)]

    def last(self, kind: str, timeout: float = 3.0) -> tuple[int, int] | None:
        deadline = time.time() + timeout
        result: tuple[int, int] | None = None
        while time.time() < deadline:
            for line in self._matching(kind):
                parts = line.split()[1:]
                try:
                    result = (int(float(parts[0])), int(float(parts[1])))
                except (IndexError, ValueError):
                    continue
            if result is not None:
                time.sleep(0.25)  # let any trailing glide events land
            else:
                time.sleep(0.05)
        return result

    def text(self, timeout: float = 3.0) -> str:
        deadline = time.time() + timeout
        out = ""
        while time.time() < deadline:
            for line in self._matching("TEXT"):
                out = line[5:]
            time.sleep(0.05)
        return out


def _screen_size() -> tuple[int, int]:
    """Monitor layout via the GTK/HUD worker if available, else X11, else FHD."""
    try:
        r = subprocess.run(
            ["/usr/bin/python3", "-c",
             "import gi;gi.require_version('Gdk','4.0');from gi.repository import Gdk;"
             "d=Gdk.Display.get_default();m=d.get_monitors();"
             "print(max(x.get_geometry().width for x in [m.get_item(i) for i in range(m.get_n_items())]),"
             + "max(x.get_geometry().height for x in [m.get_item(i) for i in range(m.get_n_items())]))"],
            capture_output=True, text=True, timeout=10,
        )
        w, h = (int(v) for v in r.stdout.split())
        return w, h
    except Exception:
        pass
    try:
        r = subprocess.run(["xrandr"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if " connected" in line and "x" in line:
                for tok in line.split():
                    if "x" in tok and tok[0].isdigit():
                        w, h = tok.split("+")[0].split("x")[:2]
                        return int(w), int(h)
    except Exception:
        pass
    return (1920, 1080)


if __name__ == "__main__":
    raise SystemExit(main())
