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
    sub.add_parser("models", help="download the speech models now (~250 MB, one time)")
    sub.add_parser("install-service", help="(re)generate the systemd unit from config.toml")

    sub.add_parser("mute", help="mute the running daemon (releases the mic)")
    sub.add_parser("unmute", help="unmute the running daemon")
    sub.add_parser("mute-toggle", help="toggle mute on the running daemon")
    sub.add_parser("state", help="query the running daemon's state")
    sub.add_parser("warm", help="pre-load the speech models and report memory use")
    sub.add_parser("unload", help="free the speech models (idle behaviour) and report memory use")
    sub.add_parser("abort", help="ABORT: kill any running action loop instantly (§10)")
    sub.add_parser("install-hotkeys", help="register GNOME hotkeys: Ctrl+Alt+M mute, Ctrl+Alt+Q abort")

    ask = sub.add_parser("ask", help="run one request through the full agent loop (text mode)")
    ask.add_argument("request", nargs="+", help="what you want Alpha to do")
    ask.add_argument("--no-speak", action="store_true", help="print the answer only")

    rec = sub.add_parser("recipes", help="manage learned deterministic macros")
    rec.add_argument("action", choices=["list", "delete", "export", "import"])
    rec.add_argument("arg", nargs="?", help="recipe id (or file for import/export)")
    rec.add_argument("dest", nargs="?", help="destination file for export")
    return p


def _cmd_init() -> int:
    from .credentials import (
        discover_opencode,
        import_provider_into_config,
    )

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


def _cmd_models() -> int:
    """One-time model download with visible progress (spec §9).

    Everything is fetched from public release URLs over HTTPS and cached under
    ~/.local/share/alpha/models; nothing is uploaded.
    """
    from . import models
    from .config import load_config

    paths.ensure_dirs()
    cfg = load_config()
    whisper = models.pick_whisper_model(cfg.stt.model)
    print(f"whisper model : {whisper}")
    total_before = sum(f.stat().st_size for f in paths.DATA_DIR.rglob("*") if f.is_file())
    try:
        models.ensure_whisper(whisper)
        print("  ✓ whisper ready")
    except Exception as e:
        print(f"  ✗ whisper failed: {e}")
    for voice in {cfg.tts.voice, cfg.tts.arabic_voice}:
        try:
            onnx, _cfg = models.ensure_piper_voice(voice)
            print(f"  ✓ piper voice {voice} ({onnx.stat().st_size / 1e6:.1f} MB)")
        except Exception as e:
            print(f"  ✗ piper voice {voice} failed: {e}")
    if cfg.assistant.wake_word.mode == "pretrained":
        try:
            models.ensure_openwakeword([cfg.assistant.wake_word.model])
            print(f"  ✓ wake word model {cfg.assistant.wake_word.model}")
        except Exception as e:
            print(f"  ✗ wake word model failed: {e}")
    else:
        # kws mode runs its own whisper-tiny matcher, independent of the
        # (usually larger) request model above.
        try:
            models.ensure_whisper("tiny")
            print("  ✓ wake word whisper-tiny (kws mode)")
        except Exception as e:
            print(f"  ✗ wake word whisper-tiny failed: {e}")
    total_after = sum(f.stat().st_size for f in paths.DATA_DIR.rglob("*") if f.is_file())
    print(f"\nmodel cache: {paths.DATA_DIR} "
          f"({total_before / 1e6:.1f} MB -> {total_after / 1e6:.1f} MB)")
    return 0


def _cmd_ask(request: str, speak: bool) -> int:
    """Text-mode request: same plan/act/verify path the microphone triggers."""
    import asyncio
    import json

    from .ipc import ctl_client

    async def run():
        return await ctl_client("ask", text=request, no_speak=not speak, timeout=600)

    try:
        reply = asyncio.run(run())
    except Exception as e:  # daemon down / timeout
        print(f"error: {e}")
        return 1
    if not reply.get("ok"):
        print(f"error: {reply.get('error', 'daemon not reachable')}")
        return 1
    print(reply.get("answer", ""))
    if reply.get("trace"):
        print("\n--- steps ---")
        for step in reply["trace"]:
            print("  " + json.dumps(step)[:200])
    return 0


def _cmd_ctl(cmd: str) -> int:
    import asyncio

    from .ipc import ctl_client

    async def run():
        # `warm` loads whisper + piper (up to a minute on a cold cache);
        # everything else answers instantly.
        return await ctl_client(cmd, timeout=300.0 if cmd == "warm" else 60.0)

    reply = asyncio.run(run())
    if not reply.get("ok"):
        print(f"error: {reply.get('error', 'daemon not reachable')}")
        return 1
    if cmd == "state":
        print(f"state={reply.get('state')} muted={reply.get('muted')} "
              f"assistant={reply.get('assistant')}")
    elif cmd == "abort":
        print("aborted — any running action loop was killed")
    elif cmd in ("warm", "unload"):
        print(f"daemon={reply.get('rss_mb', 0):.1f} MiB  "
              f"hud={reply.get('children_mb', 0):.1f} MiB  "
              f"total={reply.get('rss_mb', 0) + reply.get('children_mb', 0):.1f} MiB")
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
    if args.command == "install-service":
        from .unit import main as unit_main

        return unit_main()
    if args.command == "models":
        return _cmd_models()
    if args.command == "init":
        return _cmd_init()
    if args.command in ("mute", "unmute", "mute-toggle", "state", "abort",
                        "warm", "unload"):
        return _cmd_ctl(args.command)
        return _cmd_ctl(args.command)
    if args.command == "ask":
        return _cmd_ask(" ".join(args.request), speak=not args.no_speak)
    if args.command == "install-hotkeys":
        return _cmd_install_hotkeys()
    if args.command == "recipes":
        return _cmd_recipes(args.action, args.arg, args.dest)
    return _cmd_run()


if __name__ == "__main__":
    sys.exit(main())
