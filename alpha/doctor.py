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
from pathlib import Path

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
        lines = [ln for ln in out.splitlines() if " connected" in ln]
        return ", ".join(ln.split()[0] + " " + ln.split("(")[0].split()[-1] for ln in lines)
    # fallback: wlr-randr / gnome
    for cmd in ([["wlr-randr"], ["gnome-randr"], ["kscreen-doctor", "-o"]]):
        ok, out = _run(cmd)
        if ok and out:
            return out.replace("\n", " ")[:200]
    return "unknown"


def _audio_devices() -> str:
    ok, _out = _run(["pactl", "info"])
    if not ok:
        return "pactl unavailable"
    sink_ok, sinks = _run(["pactl", "list", "short", "sources"])
    src_ok, srcs = _run(["pactl", "list", "short", "sinks"])
    def names(s):
        return ", ".join(
            part[1] if len(part := ln.split("\t")) > 1 else ln
            for ln in s.splitlines() if ln
        )
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

    # 8. M2 audio/models
    _check_audio_pipeline(cfg, add)

    # 9. M4 input + safety
    import os as _os

    uinput_ok = _os.access("/dev/uinput", _os.W_OK)
    add("input backend (/dev/uinput)", uinput_ok,
        "native uinput (Wayland+X11)" if uinput_ok else
        "no write access — add user to 'input' group and re-login",
        "ok" if uinput_ok else "fail")
    try:
        import subprocess

        r = subprocess.run(
            ["gsettings", "get", "org.gnome.settings-daemon.plugins.media-keys",
             "custom-keybindings"], capture_output=True, text=True, timeout=5)
        has_abort = "alpha-abort" in (r.stdout or "")
        add("abort hotkey (Ctrl+Alt+Q)", has_abort,
            "registered" if has_abort else "not registered — run `alpha install-hotkeys`",
            "ok" if has_abort else "warn")
    except Exception:
        add("abort hotkey (Ctrl+Alt+Q)", True, "not GNOME (gsettings unavailable)", "skip")

    # 10. M5 vision: portal/screen-sharing capability
    _check_vision(add, live=network)

    # 11. M8: measured memory of the running daemon, if it is up
    _check_memory_running(add)

    # 12. systemd unit drift: config.toml vs what systemd enforces
    _check_unit_drift(add)

    # 12. wake-word engine, stated with its real cost
    if cfg is not None:
        ww = cfg.assistant.wake_word
        if ww.mode == "pretrained":
            add("wake word engine", True,
                f"openWakeWord '{ww.model}' (~205 MiB idle daemon)")
        else:
            add("wake word engine", True,
                f"whisper-tiny KWS for {ww.phrase!r} (~390 MiB idle daemon; "
                "`pretrained` is lighter, train a model for a custom phrase)",
                "warn")

    return report


def _check_vision(add, live: bool = True) -> None:
    """Report the three vision layers, honestly and without side effects."""
    # AT-SPI: available through the HUD's system python (GTK4 + Atspi live there)
    # Fedora installs these in /usr/libexec (not $PATH), and Alpha reaches them
    # through the HUD's system python — a PATH lookup reports a false negative.
    atspi = any(Path(p).exists() for p in (
        "/usr/libexec/at-spi-bus-launcher",
        "/usr/libexec/at-spi2-registryd",
        "/usr/lib/at-spi2-core/at-spi-bus-launcher",
        "/usr/lib64/at-spi2-core/at-spi-bus-launcher",
    ))
    add("AT-SPI (element table)", atspi,
        "available via the HUD worker (/usr/libexec)" if atspi else
        "at-spi2-core not found — the element table will be empty",
        "ok" if atspi else "warn")

    # Portal ScreenCast: the capability that needs one-time user consent
    portal = Path("/usr/share/dbus-1/services/org.freedesktop.impl.portal.desktop.gnome.service")
    portal_bin = shutil.which("xdg-desktop-portal")
    add("portal screen sharing", bool(portal_bin or portal.exists()),
        "approve the HUD 'Share' dialog once to enable screenshots"
        if (portal_bin or portal.exists()) else
        "xdg-desktop-portal not installed — screenshots unavailable",
        "ok" if (portal_bin or portal.exists()) else "warn")

    # GStreamer + pipewire: what actually pulls frames out of the portal fd
    missing = [m for m in ("gst-launch-1.0", "pipewire") if not shutil.which(m)]
    add("screenshot pipeline", not missing,
        "GStreamer pipewiresrc ready" if not missing else
        f"missing: {', '.join(missing)} (dnf install gstreamer1-plugins-good pipewire-gstreamer)",
        "ok" if not missing else "warn")

    # Vision-capable model: probed only when asked (network round trip)
    if not live:
        add("vision-capable model", True, "skipped (no network)", "skip")
        return
    try:
        from .brain.providers.openai_compatible import OpenAICompatibleProvider
        from .config import load_config

        cfg = load_config()
        prov = OpenAICompatibleProvider(cfg.llm.planner)
        if not hasattr(prov, "supports_vision"):
            add("vision-capable model", True,
                "provider has no image probe — pixel grounding assumed off", "warn")
        else:
            ok = bool(prov.supports_vision())
            add("vision-capable model", ok,
                "screenshots will be sent to the provider" if ok else
                f"text-only grounding ({cfg.llm.planner.model} refused a test image; "
                "use a vision model if you want pixel grounding)",
                "ok" if ok else "warn")
    except Exception as e:
        add("vision-capable model", True, f"could not probe: {type(e).__name__}: {e}",
            "warn")


def _check_unit_drift(add) -> None:
    """The unit is generated from config; report when the deployed copy is stale."""
    try:
        from . import unit as unit_mod

        cfg_limit, deployed = unit_mod.drift()
        if deployed is None:
            add("systemd unit", True,
                f"not deployed (expected MemoryMax {cfg_limit} MB) — "
                "run `alpha install-service`", "warn")
        elif cfg_limit is not None and deployed != cfg_limit:
            add("systemd unit", False,
                f"MemoryMax {deployed} MB but config says {cfg_limit} MB — "
                "run `alpha install-service`", "warn")
        else:
            add("systemd unit", True, f"MemoryMax {deployed} MB (matches config)")
    except Exception as e:
        add("systemd unit", True, f"could not check: {type(e).__name__}", "warn")


def _check_memory_running(add) -> None:
    """Read the live daemon's RSS the same way `alpha state`/soak does."""
    sock = paths.STATE_DIR / "ctl.sock"
    if not sock.exists():
        add("running daemon memory", True, "daemon not running", "skip")
        return
    try:
        pid = _daemon_pid()
        if not pid:
            add("running daemon memory", True, "daemon pid not found", "skip")
            return
        daemon_mb = _rss_mb(pid)
        children = _child_pids(pid)
        child_mb = sum(_rss_mb(c) for c in children)
        total = daemon_mb + child_mb
        ceiling = 1024
        try:
            from .config import load_config

            ceiling = load_config().resources.memory_max_mb
        except Exception:
            pass
        detail = (f"daemon {daemon_mb:.0f} MiB + {len(children)} child "
                  f"{child_mb:.0f} MiB = {total:.0f} MiB (MemoryMax {ceiling} MB)")
        add("running daemon memory", total < ceiling * 0.9, detail,
            "ok" if total < ceiling * 0.9 else "warn")
    except Exception as e:
        add("running daemon memory", True, f"unavailable: {type(e).__name__}", "warn")


def _daemon_pid() -> int | None:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "alpha", "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=5)
        pid = int((out.stdout or "0").strip() or 0)
        return pid or None
    except Exception:
        return None


def _child_pids(pid: int) -> list[int]:
    try:
        return [int(x) for x in (Path(f"/proc/{pid}/task/{pid}/children")
                                 .read_text().split())]
    except Exception:
        return []


def _rss_mb(pid: int) -> float:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def _check_audio_pipeline(cfg: Config | None, add) -> None:
    from . import models as models_mod

    if cfg is None:
        add("audio pipeline", False, "no config", "fail")
        return

    # mic + default input visible
    try:
        import sounddevice as sd

        _, _idx = sd.default.device
        dev = sd.query_devices(kind="input")
        add("default input device", True, dev["name"])
    except Exception as e:
        add("default input device", False, redact(str(e))[:120], "fail")

    # wake word
    w = cfg.assistant.wake_word
    if w.mode == "kws":
        add("wake word", True, f"kws phrase={w.phrase!r} (works for any name)")
    else:
        from .wake import BUNDLED_WAKEWORDS

        ok = w.model in BUNDLED_WAKEWORDS or (paths.WAKEWORD_DIR / f"{w.model}.onnx").exists()
        add("wake word model", ok,
            f"{w.model} (pretrained)" if ok else f"{w.model} not found", "ok" if ok else "fail")

    # STT model presence (tiny downloaded lazily — warn rather than fail)
    model = models_mod.pick_whisper_model(cfg.stt.model)
    hf = Path.home() / ".cache" / "huggingface" / "hub"
    present = any(model.split("/")[-1] in p.name for p in hf.glob("models--*")) if hf.exists() else False
    add("whisper STT model", True, f"{model} ({'downloaded' if present else 'downloads on first run'})",
        "ok" if present else "warn")

    # piper voices
    for v in (cfg.tts.voice, cfg.tts.arabic_voice):
        d = paths.MODEL_DIR / "piper" / v
        present = (d / f"{v}.onnx").exists()
        add(f"piper voice {v}", present, "downloaded" if present else "will download on first use",
            "ok" if present else "warn")

    # VAD
    from .listen import Recorder

    rec = Recorder()
    add("voice activity detector", rec.vad is not None, "silero" if rec.vad else "energy fallback",
        "ok" if rec.vad else "warn")


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
        r = httpx.post(url, json=body, headers=headers, timeout=45)
        if r.status_code == 200:
            add("provider reachability", True, f"{role.provider}/{role.model}")
        elif r.status_code in (401, 402, 403):
            # Auth/quota problems are actionable and must not be mistaken for an outage.
            detail = f"HTTP {r.status_code}: {redact(r.text[:140])}"
            hint = (" — check the key with `alpha init`" if r.status_code in (401, 403)
                    else " — provider quota/credit exhausted; switch model or provider")
            add("provider reachability", False, detail + hint, "fail")
        else:
            detail = f"HTTP {r.status_code}: {redact(r.text[:120])}"
            add("provider reachability", False, detail, "fail")
    except httpx.TimeoutException:
        # The free tiers used in testing regularly take >15 s for a single call;
        # a slow provider is usable, so this is a warning with the real cause.
        add("provider reachability", True,
            f"timed out after 45s — {role.provider} is slow or unreachable; "
            "Alpha still works, but expect long waits", "warn")
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
