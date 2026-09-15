"""Central path definitions for Alpha.

XDG-style layout:
  config: ~/.config/alpha/config.toml
  state:  ~/.local/state/alpha/   (logs, actions.jsonl)
  data:   ~/.local/share/alpha/   (models, wakewords, recipes)
"""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(env: str, default: Path) -> Path:
    value = os.environ.get(env)
    return Path(value) if value else default


HOME = Path.home()
CONFIG_DIR = _xdg("XDG_CONFIG_HOME", HOME / ".config") / "alpha"
CONFIG_FILE = CONFIG_DIR / "config.toml"

STATE_DIR = _xdg("XDG_STATE_HOME", HOME / ".local" / "state") / "alpha"
LOG_DIR = STATE_DIR / "logs"
ACTIONS_LOG = STATE_DIR / "actions.jsonl"
SPEND_LOG = STATE_DIR / "spend.jsonl"

DATA_DIR = _xdg("XDG_DATA_HOME", HOME / ".local" / "share") / "alpha"
WAKEWORD_DIR = DATA_DIR / "wakewords"
MODEL_DIR = DATA_DIR / "models"
RECIPE_DIR = DATA_DIR / "recipes"


def ensure_dirs() -> None:
    """Create every directory Alpha owns, with safe permissions."""
    for d in (CONFIG_DIR, STATE_DIR, LOG_DIR, DATA_DIR, WAKEWORD_DIR, MODEL_DIR, RECIPE_DIR):
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(0o700)
        except OSError:
            pass
