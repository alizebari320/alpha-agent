"""Render and install the systemd user unit from the config (M8).

Why this module exists: `memory_max_mb` in `config.toml` is documented as
"mirrored into the systemd MemoryMax", but the unit was a static file — editing
the config changed nothing, and the deployed limit silently stayed at the
template value. Measured consequence: a warm daemon (1003 MiB) plus the HUD
(173 MiB) in one cgroup is 1176 MiB, so a 1024 MB `MemoryMax` OOM-kills Alpha
mid-request. This module makes the promise true and lets `alpha doctor` report
drift between the config and what systemd is actually enforcing.
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

UNIT_NAME = "alpha.service"
TEMPLATE = Path(__file__).resolve().parent.parent / "scripts" / "alpha.service"


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def render(exec_start: str | None = None, memory_max_mb: int | None = None,
           repo_dir: Path | None = None) -> str:
    """Build the unit text from the template plus the config's resource budget."""
    from .config import load_config

    cfg = load_config()
    repo = repo_dir or Path(__file__).resolve().parent.parent
    if exec_start is None:
        exec_start = str(repo / ".venv" / "bin" / "alpha")
    if memory_max_mb is None:
        memory_max_mb = cfg.resources.memory_max_mb

    text = TEMPLATE.read_text()
    text = re.sub(r"^ExecStart=.*$", f"ExecStart={exec_start}", text, flags=re.M)
    text = re.sub(r"^WorkingDirectory=.*$", f"WorkingDirectory={repo}", text, flags=re.M)
    text = re.sub(r"^MemoryMax=.*$", f"MemoryMax={int(memory_max_mb)}M", text, flags=re.M)
    return text


def deployed_memory_max() -> int | None:
    """The MemoryMax systemd is actually enforcing, or None if no unit is installed."""
    p = unit_path()
    if not p.exists():
        return None
    m = re.search(r"^MemoryMax=(\d+)M", p.read_text(), flags=re.M)
    return int(m.group(1)) if m else None


def install(restart: bool = False) -> tuple[bool, str]:
    """Write the unit from the current config, then daemon-reload.

    Returns (changed, message). Never raises: a failure to install the unit must
    not stop the daemon from running.
    """
    try:
        text = render()
        p = unit_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        changed = (not p.exists()) or p.read_text() != text
        if changed:
            p.write_text(text)
        r = subprocess.run(["systemctl", "--user", "daemon-reload"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return changed, f"daemon-reload failed: {r.stderr.strip()[:200]}"
        if changed and restart:
            subprocess.run(["systemctl", "--user", "restart", "alpha"],
                           capture_output=True, text=True, timeout=20)
        return changed, ("unit updated" if changed else "unit already current")
    except Exception as e:
        return False, f"could not install unit: {type(e).__name__}: {e}"


def drift() -> tuple[int | None, int | None]:
    """(config_limit, deployed_limit) — differing values mean the unit is stale."""
    from .config import load_config

    try:
        cfg_limit = load_config().resources.memory_max_mb
    except Exception:
        return None, deployed_memory_max()
    return cfg_limit, deployed_memory_max()


def main() -> int:
    """`alpha install-service` — make systemd match the config."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    paths.ensure_dirs()
    cfg_limit, before = drift()
    changed, msg = install(restart=True)
    _, after = drift()
    print(f"unit: {unit_path()}")
    print(f"MemoryMax: {before if before is not None else 'not deployed'} MB "
          f"-> {after} MB (config says {cfg_limit} MB)")
    print(msg + (" (restarted alpha)" if changed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
