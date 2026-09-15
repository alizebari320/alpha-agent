"""Safety guard (§10): abort hotkey, allowlist, audit trail.

- ABORT: `alpha abort` (CLI) or the abort hotkey must kill any running loop
  INSTANTLY. On Wayland the hotkey is a GNOME custom keybinding (registered by
  `alpha install-hotkeys`); on X11 the daemon can also grab keys directly.
- AUDIT: every action is appended to ~/.local/state/alpha/actions.jsonl BEFORE
  it executes (spec hard constraint) — so a crash mid-action leaves a trail.
- ALLOWLIST: bash commands outside the allowlist require spoken confirmation
  (destructive commands ALWAYS do).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from .. import paths
from ..config import SafetyConfig

log = logging.getLogger(__name__)

# commands that are destructive no matter what (§10)
DESTRUCTIVE_RE = re.compile(
    r"(^|\s|;|&&|\|)(sudo|rm\b|rmdir|dd\b|mkfs|shutdown|reboot|systemctl\s+(stop|disable|mask)|"
    r"git\s+push\s+.*-f|git\s+push\s+--force|gsettings\s+reset|dconf\s+reset|truncate|shred)",
    re.IGNORECASE,
)


class AbortRequested(Exception):
    """Raised inside the action loop when abort fires."""


class AuditLog:
    """Append-only JSONL of every action, written BEFORE execution."""

    def __init__(self, path: Path | None = None):
        self.path = path or paths.ACTIONS_LOG
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, action: dict) -> None:
        entry = {"ts": time.time(), **action}
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            log.error("audit log write failed: %s", e)
        log.info("action> %s", json.dumps(action, ensure_ascii=False)[:200])


class SafetyGuard:
    def __init__(self, cfg: SafetyConfig, audit: AuditLog | None = None):
        self.cfg = cfg
        self.audit = audit or AuditLog()
        self._aborted = asyncio.Event()

    # ------------------------------------------------------------- abort

    def request_abort(self, reason: str = "user") -> None:
        """Called by the CLI/hotkey path. Instantly sets the abort flag."""
        self._aborted.set()
        self.audit.record({"tool": "abort", "reason": reason})
        log.warning("ABORT requested (%s)", reason)

    def check_abort(self) -> None:
        """Call between loop steps; raises AbortRequested."""
        if self._aborted.is_set():
            raise AbortRequested()

    def clear_abort(self) -> None:
        self._aborted.clear()

    @property
    def abort_requested(self) -> bool:
        return self._aborted.is_set()

    # --------------------------------------------------------- gating

    def check_action(self, tool: str, args: dict) -> tuple[bool, str]:
        """Return (allowed_without_confirmation, reason)."""
        # denylisted apps: refuse to act entirely
        target = str(args.get("app") or args.get("window") or "").lower()
        for d in self.cfg.denylist:
            if d.lower() in target:
                return False, f"app {d!r} is on the denylist"
        if tool == "bash":
            cmd = str(args.get("command", ""))
            if DESTRUCTIVE_RE.search(cmd):
                return False, "destructive command — confirmation required"
            base = cmd.strip().split()[0] if cmd.strip() else ""
            if base in {a.split()[0] for a in self.cfg.bash_allowlist}:
                return True, "allowlisted"
            return False, "command not in allowlist — confirmation required"
        if tool == "type_text" and args.get("_password_field"):
            return False, "refusing to type into a password field"
        return True, "default"

    def audit_action(self, tool: str, args: dict) -> None:
        safe_args = {k: v for k, v in args.items() if not str(k).startswith("_")}
        self.audit.record({"tool": tool, **safe_args})
