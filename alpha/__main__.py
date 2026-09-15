"""Entry point: ``alpha`` CLI.

Subcommands:
  (none)  run the daemon in the foreground (what systemd uses)
  doctor  print diagnostics (``alpha doctor``)
  init    first-run wizard: import credentials from opencode, pick a name
  --version
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__, paths
from .log import configure_logging, redact

log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alpha", description="Voice-driven computer-use agent")
    p.add_argument("--version", action="version", version=f"alpha {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command")

    d = sub.add_parser("doctor", help="run diagnostics")
    d.add_argument("--no-network", action="store_true", help="skip the API reachability check")

    sub.add_parser("init", help="first-run wizard (import credentials, set name)")
    return p


def _cmd_init() -> int:
    from .credentials import (PREFERRED_PROVIDER_KEYS, Provider, discover_opencode,
                              import_provider_into_config, store_key)
    from .config import Config, ConfigError, parse_config

    paths.ensure_dirs()

    discovered = discover_opencode()
    if not discovered.providers:
        print("No opencode providers found. Scan ~/.config/opencode/opencode.json failed.")
        print("You can still author ~/.config/alpha/config.toml manually (see config.toml.example).")
        return 1

    print("Discovered opencode providers:")
    for p in discovered.providers:
        has_key = "key" if p.api_key else "no-key"
        print(f"  - {p.id:<22} {p.model:<24} {p.base_url} [{has_key}]")

    # Pick a provider, preferring the known-good default.
    chosen = discovered.chosen
    if chosen is None:
        chosen = next((p for p in discovered.providers if p.api_key), None)
    if chosen is None:
        print("\nNone of the discovered providers have an API key; nothing to import.")
        return 1

    print(f"\nUsing default provider: {chosen.id}  model: {chosen.model}")
    print(f"  base URL: {chosen.base_url}")
    print(f"  user-agent (required by this gateway): {chosen.user_agent}")

    if paths.CONFIG_FILE.exists():
        print(f"\nExisting config found at {paths.CONFIG_FILE}; updating its [llm] block.")
    import_provider_into_config(chosen, paths.CONFIG_FILE)
    print(f"API key stored in system keyring (service={chosen.id}).")
    print(f"Wrote {paths.CONFIG_FILE}")
    print("\nOverride anything you like by editing that file, then run `alpha doctor`.")
    return 0


def _cmd_doctor(network: bool) -> int:
    from .config import load_config
    from .doctor import main as doctor_main

    try:
        cfg = load_config()
    except Exception as e:
        cfg = None
        print(f"[WARN] {redact(str(e))}", file=sys.stderr)
    return doctor_main(cfg, network=network)


def _cmd_run() -> int:
    from .daemon import main_async

    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    configure_logging(args.verbose)

    if args.command == "doctor":
        return _cmd_doctor(not args.no_network)
    if args.command == "init":
        return _cmd_init()
    return _cmd_run()


if __name__ == "__main__":
    sys.exit(main())