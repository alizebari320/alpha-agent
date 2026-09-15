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
from pathlib import Path

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

    m = sub.add_parser("mute", help="mute the running daemon (releases the mic)")
    sub.add_parser("unmute", help="unmute the running daemon")
    sub.add_parser("mute-toggle", help="toggle mute on the running daemon")
    sub.add_parser("state", help="query the running daemon's state")
    sub.add_parser("abort", help="ABORT: kill any running action loop instantly (§10)")
    sub.add_parser("install-hotkeys", help="register GNOME hotkeys: Ctrl+Alt+M mute, Ctrl+Alt+Q abort")

    rec = sub.add_parser("recipes", help="manage learned deterministic macros")
    rec.add_argument("action", choices=["list", "delete", "export", "import"])
    rec.add_argument("arg", nargs="?", help="recipe id (or file for import/export)")
    rec.add_argument("dest", nargs="?", help="destination file for export")
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


def _cmd_ctl(cmd: str) -> int:
    import asyncio

    from .ipc import ctl_client

    async def run():
        return await ctl_client(cmd)

    reply = asyncio.run(run())
    if not reply.get("ok"):
        print(f"error: {reply.get('error', 'daemon not reachable')}")
        return 1
    if cmd == "state":
        print(f"state={reply.get('state')} muted={reply.get('muted')} "
              f"assistant={reply.get('assistant')}")
    elif cmd == "abort":
        print("aborted — any running action loop was killed")
    else:
        print(f"muted={reply.get('muted')}")
    return 0


def _cmd_install_hotkeys() -> int:
    """Register GNOME custom keybindings (user-level gsettings, no sudo):
      Ctrl+Alt+M -> alpha mute-toggle
      Ctrl+Alt+Q -> alpha abort   (spec §10 global abort)

    On GNOME Wayland apps cannot grab global keys; session keybindings running
    a command are the supported mechanism. On X11 the daemon could also grab
    keys itself; the keybinding works everywhere GNOME runs.
    """
    import subprocess

    base = "org.gnome.settings-daemon.plugins.media-keys"
    def gs(*args):
        return subprocess.run(["gsettings", *args], capture_output=True, text=True)

    if gs("get", base, "custom-keybindings").returncode != 0:
        print("gsettings/GNOME not available")
        return 1

    cmd = f"{sys.executable} -m alpha"
    bindings = {
        "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/alpha-mute/":
            ("Alpha mute", f"{cmd} mute-toggle", "<Primary><Alt>m"),
        "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/alpha-abort/":
            ("Alpha ABORT", f"{cmd} abort", "<Primary><Alt>q"),
    }

    cur = gs("get", base, "custom-keybindings").stdout.strip()
    paths_list = [p for p in bindings if p not in cur]
    if paths_list:
        import ast

        try:
            existing = ast.literal_eval(cur)
        except Exception:
            existing = []
        new = list(existing) + paths_list
        gs("set", base, "custom-keybindings", repr(new))

    for path, (name, command, binding) in bindings.items():
        kb = base + ".custom-keybinding:" + path
        gs("set", kb, "name", name)
        gs("set", kb, "command", command)
        gs("set", kb, "binding", binding)
        print(f"registered {binding} -> {name}")
    print("Remove anytime in Settings > Keyboard > View and Customize Shortcuts.")
    return 0


def _cmd_recipes(action: str, arg: str | None, dest: str | None) -> int:
    from alpha.brain.recipes import RecipeStore

    store = RecipeStore()
    if action == "list":
        rs = store.all()
        if not rs:
            print("no recipes yet — they appear automatically after successful tasks")
            return 0
        for r in rs:
            print(f"{r.id}  runs={r.runs:<3} {r.display_request[:60]!r} "
                  f"({len(r.actions)} actions)")
        return 0
    if action == "delete":
        if not arg or not store.delete(arg):
            print(f"no recipe {arg!r}")
            return 1
        print("deleted")
        return 0
    if action == "export":
        from pathlib import Path

        out = store.export(arg, Path(dest or f"{arg}.json"))
        if not out:
            print(f"no recipe {arg!r}")
            return 1
        print(f"exported to {out}")
        return 0
    if action == "import":
        from pathlib import Path

        r = store.import_from(Path(arg))
        if not r:
            return 1
        print(f"imported {r.id}: {r.display_request!r}")
        return 0
    return 1


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
    if args.command in ("mute", "unmute", "mute-toggle", "state", "abort"):
        return _cmd_ctl(args.command)
    if args.command == "install-hotkeys":
        return _cmd_install_hotkeys()
    if args.command == "recipes":
        return _cmd_recipes(args.action, args.arg, args.dest)
    return _cmd_run()


if __name__ == "__main__":
    sys.exit(main())