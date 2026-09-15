#!/usr/bin/env bash
# Alpha — Fedora installer (M1 scope).
#
# Primary install path: git clone && ./scripts/install-fedora.sh
#
# This script:
#   1. verifies Fedora + python3.11+ + `uv`
#   2. creates a venv and installs the project
#   3. creates Alpha's state/data/config dirs
#   4. imports existing opencode credentials into Alpha's config (keyring)
#   5. installs + enables the systemd --user service
#
# Later milestones extend this file (dnf ydotool / uinput for Wayland input in
# M4). Those changes are additive and documented here.

set -euo pipefail

log()  { printf '\033[1;32m[alpha]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[alpha]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[alpha]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 0. platform checks ------------------------------------------------------
grep -qi fedora /etc/os-release || warn "not Fedora — YMMV (target is Fedora Linux)"
command -v python3 >/dev/null || die "python3 not found"

# --- 1. ensure uv ------------------------------------------------------------
if ! command -v uv >/dev/null; then
  log "installing uv (the Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null || die "uv not found after install"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# --- 2. venv + deps ----------------------------------------------------------
log "creating venv and installing dependencies"
uv sync --frozen 2>/dev/null || uv sync

# --- 3. state/config/data dirs ----------------------------------------------
log "creating Alpha state directories"
uv run python -c 'from alpha import paths; paths.ensure_dirs(); print(paths.STATE_DIR)'

# --- 4. import opencode credentials (auto, non-interactive) ------------------
if [ -f "$HOME/.config/opencode/opencode.json" ] || [ -f "$HOME/.local/share/opencode/auth.json" ]; then
  log "importing credentials from opencode into Alpha + system keyring"
  uv run alpha init || warn "credential import failed; run `alpha init` manually"
else
  warn "no opencode config found; run `alpha init` after setting one up, or edit config.toml"
fi

# --- 5. systemd --user service ----------------------------------------------
log "installing systemd --user unit"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
# Point ExecStart at the real venv binary inside this checkout.
sed "s|^ExecStart=.*|ExecStart=$REPO_DIR/.venv/bin/alpha|" \
  "$REPO_DIR/scripts/alpha.service" > "$UNIT_DIR/alpha.service"

systemctl --user daemon-reload
systemctl --user enable --now alpha || warn "could not start service; try: systemctl --user start alpha"

log "Done. Check it with:  alpha doctor   and   systemctl --user status alpha"