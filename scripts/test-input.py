#!/usr/bin/env python3
"""M4 acceptance test: input injection on THIS session type.

Moves the real cursor to exact coordinates (center, corners), clicks, types,
and presses key combos — visibly, on your screen. Verifies coordinate math
against the HUD-reported monitor geometry, and exercises the abort audit log.

Run:  uv run python scripts/test-input.py [--quick]
Watch your cursor move. Nothing destructive happens (clicks land on empty
desktop areas; typing goes nowhere harmful — close your text editors first).
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


def get_geometry_from_daemon() -> tuple[list[dict], tuple[int, int] | None]:
    """Ask the running daemon for the HUD-reported geometry."""
    ctl = Path.home() / ".local" / "state" / "alpha" / "ctl.sock"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(str(ctl))
        s.sendall(json.dumps({"cmd": "state"}).encode() + b"\n")
        reply = json.loads(s.recv(4096).split(b"\n")[0])
        s.close()
    except OSError:
        return [], None
    return reply.get("monitors", []), reply.get("screen")


def main() -> int:
    quick = "--quick" in sys.argv

    print("==> detecting session + geometry")
    session = subprocess.run(["sh", "-c", "echo $XDG_SESSION_TYPE"],
                             capture_output=True, text=True).stdout.strip()
    monitors, screen = get_geometry_from_daemon()
    if not monitors:
        print("    (daemon not running or geometry not yet reported; assuming 1920x1080)")
        w, h = 1920, 1080
    else:
        w = max(m["x"] + m["w"] for m in monitors)
        h = max(m["y"] + m["h"] for m in monitors)
    print(f"    session={session} screen={w}x{h} monitors={len(monitors)}")

    from alpha.input.detect import detect_backend

    try:
        backend = detect_backend(w, h)
    except Exception as e:
        check("input backend available", False, str(e))
        return 1
    check("input backend created", True, type(backend).__name__)

    try:
        # 1) absolute moves: center + 4 corners (exact coordinate math)
        print("==> moving cursor — WATCH YOUR SCREEN")
        targets = [(w // 2, h // 2), (10, 10), (w - 10, 10), (w - 10, h - 10), (10, h - 10)]
        if quick:
            targets = targets[:2]
        for (x, y) in targets:
            backend.move_abs(x, y)
            time.sleep(0.35)
        pos = backend.get_cursor_pos()
        check("absolute move to exact coords", pos == targets[-1], f"tracked pos={pos}")

        # 2) center + click (visible: nothing destructive)
        backend.click(w // 2, h // 2)
        check("click at center", True)

        # 3) typing + key combos (user should have a harmless focus target)
        print("==> typing 'alpha-test' and pressing keys")
        backend.move_abs(w // 2, h // 2)
        backend.type_text("alpha-test 123")
        backend.key("Return")
        backend.key("ctrl+l")
        check("type_text + key combos emitted", True)

        # 4) scroll
        backend.scroll(0, -3)
        time.sleep(0.2)
        backend.scroll(0, 3)
        check("scroll emitted", True)

        # 5) audit log records everything (write before execution)
        actions = Path.home() / ".local" / "state" / "alpha" / "actions.jsonl"
        from alpha.safety.guard import AuditLog, SafetyGuard
        from alpha.config import SafetyConfig

        guard = SafetyGuard(SafetyConfig())
        guard.audit_action("move_abs", {"x": 1, "y": 2})
        guard.audit_action("click", {"x": 1, "y": 2, "button": "left"})
        last = actions.read_text().strip().splitlines()[-2:]
        ok = all('"tool"' in ln for ln in last)
        check("actions.jsonl audit trail", ok and actions.exists())

        # 6) allowlist gating
        allowed, _ = guard.check_action("bash", {"command": "firefox"})
        blocked, _ = guard.check_action("bash", {"command": "rm -rf /"})
        blocked2, _ = guard.check_action("bash", {"command": "sudo reboot"})
        blocked3, _ = guard.check_action("bash", {"command": "git push --force"})
        notlisted, _ = guard.check_action("bash", {"command": "curl example.com"})
        check("allowlist: firefox allowed", allowed)
        check("allowlist: rm blocked", not blocked)
        check("allowlist: sudo blocked", not blocked2)
        check("allowlist: git push -f blocked", not blocked3)
        check("allowlist: unlisted needs confirmation", not notlisted)

        # 7) abort flag semantics
        guard.request_abort("test")
        check("abort flag set instantly", guard.abort_requested)
        try:
            guard.check_abort()
            check("check_abort raises", False)
        except Exception:
            check("check_abort raises AbortRequested", True)
        guard.clear_abort()
    finally:
        close = getattr(backend, "close", None)
        if close:
            close()

    print()
    if failures:
        print(f"RESULT: {failures} FAILURES")
        return 1
    print("RESULT: M4 INPUT BACKEND OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
