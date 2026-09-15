"""`alpha doctor` diagnostics.

One command that answers 90% of future support issues: audio devices, uinput
permissions, session type, keyring, API key validity, model presence, monitor
layout, state-dir writability, recipe store integrity. Early-milestone checks
that aren't implemented yet report as "not implemented" rather than failing.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

import httpx

from . import paths
from .config import Config, load_config
from .credentials import get_key, keyring_available
from .log import redact

log = logging.getLogger(__name__)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    level: str = "ok"  # ok | warn | fail | skip


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(c.level == "fail" for c in self.checks)

    def render(self) -> str:
        lines = ["=== alpha doctor ==="]
        for c in self.checks:
            icon = {"ok": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}[c.level]
            lines.append(f"[{icon}] {c.name}" + (f" — {c.detail}" if c.detail else ""))
        lines.append("RESULT: " + ("FAIL" if self.failed else "OK"))
        return "\n".join(lines)


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return out.returncode == 0, (out.stdout or out.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def _session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "unknown")


def _monitors() -> str:
    # X11 and Wayland (wlroots) both often expose xrandr; GNOME Wayland may not.
    ok, out = _run(["xrandr", "--query"])
    if ok and out:
        lines = [l for l in out.splitlines() if " connected" in l]
        return ", ".join(l.split()[0] + " " + l.split("(")[0].split()[-1] for l in lines)
    # fallback: wlr-randr / gnome
    for cmd in ([["wlr-randr"], ["gnome-randr"], ["kscreen-doctor", "-o"]]):
        ok, out = _run(cmd)
        if ok and out:
            return out.replace("\n", " ")[:200]
    return "unknown"


def _audio_devices() -> str:
    ok, out = _run(["pactl", "info"])
    if not ok:
        return "pactl unavailable"
    sink_ok, sinks = _run(["pactl", "list", "short", "sources"])
    src_ok, srcs = _run(["pactl", "list", "short", "sinks"])
    def names(s):
        return ", ".join(l.split("\t")[1] if len(l.split("\t")) > 1 else l for l in s.splitlines() if l)
    return f"inputs=[{names(srcs if src_ok else '')}] outputs=[{names(sinks if sink_ok else '')}]"


def run_doctor(cfg: Config | None, network: bool = True) -> DoctorReport:
    report = DoctorReport()

    def add(name, ok, detail="", level=None):
        report.checks.append(Check(name, ok, detail, level or ("ok" if ok else "fail")))

    # 1. python
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    add("python version", sys.version_info >= (3, 11), py,
        level="ok" if sys.version_info >= (3, 11) else "fail")

    # 2. config
    if cfg is None:
        add("config", False, f"missing/invalid at {paths.CONFIG_FILE}", "fail")
    else:
        add("config", True, f"assistant={cfg.assistant.name} model={cfg.llm.planner.model}")

    # 3. keyring + key
    ok, backend = keyring_available()
    add("keyring", ok, backend)
    if cfg is not None:
        key = get_key(cfg.llm.planner.api_key_keyring_user)
        add("api key in keyring", bool(key),
            f"provider={cfg.llm.planner.provider}" if key else "not found — run `alpha init`")

    # 4. session type + monitors
    st = _session_type()
    add("session type", st in ("x11", "wayland"), st,
        level="ok" if st in ("x11", "wayland") else "warn")
    add("monitors", True, _monitors())

    # 5. audio devices
    audio = _audio_devices()
    add("audio devices", "pactl unavailable" not in audio, audio[:240],
        level="ok" if "pactl unavailable" not in audio else "warn")

    # 6. provider reachability (skipped when offline / --no-network)
    if cfg is not None and network:
        _check_provider(cfg, add)
    else:
        add("provider reachability", True, "skipped (no network)", "skip")

    # 7. state dirs writable
    paths.ensure_dirs()
    writable = os.access(paths.STATE_DIR, os.W_OK)
    add("state dirs", writable, str(paths.STATE_DIR))

    # 8. future milestones
    add("ydotoold / uinput", True, "M4 (input injection) — not yet implemented", "skip")
    add("AT-SPI accessibility", True, "M5 (vision) — not yet implemented", "skip")
    add("wake word model", True, "M2 (audio) — not yet implemented", "skip")

    return report


def _check_provider(cfg: Config, add) -> None:
    role = cfg.llm.planner
    key = get_key(role.api_key_keyring_user)
    if not key:
        add("provider reachability", False, "no API key", "fail")
        return
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # NB: AgentRouter (and similar gateways) whitelist clients by User-Agent.
        "User-Agent": role.user_agent,
    }
    url = role.base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": role.model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 8,
    }
    try:
        r = httpx.post(url, json=body, headers=headers, timeout=15)
        if r.status_code == 200:
            add("provider reachability", True, f"{role.provider}/{role.model}")
        else:
            detail = f"HTTP {r.status_code}: {redact(r.text[:120])}"
            add("provider reachability", False, detail, "fail")
    except Exception as e:
        add("provider reachability", False, redact(str(e))[:160], "fail")


def main(cfg: Config | None = None, network: bool = True) -> int:
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:
            cfg = None
    report = run_doctor(cfg, network=network)
    print(report.render())
    return 1 if report.failed else 0