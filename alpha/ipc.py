"""IPC plumbing for the HUD and the control socket.

- HUDServer: unix socket at ~/.local/state/alpha/hud.sock; the daemon pushes
  JSON lines {"type":"state"|"mute", ...} to every connected HUD process.
- CtlServer: unix socket at ~/.local/state/alpha/ctl.sock; CLI (`alpha mute`,
  `alpha state`) and the HUD's mute button send {"cmd": ...} here.
- spawn_hud(): run alpha/hud/app.py with the SYSTEM python (it needs
  PyGObject+GTK4, which only the distro python ships).

All sockets are created with mode 0600 (user-only) under XDG_STATE_HOME.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

HUD_SOCK = paths.STATE_DIR / "hud.sock"
CTL_SOCK = paths.STATE_DIR / "ctl.sock"
HUD_APP = Path(__file__).parent / "hud" / "app.py"

# system python ships PyGObject; prefer it explicitly
SYSTEM_PYTHONS = ["/usr/bin/python3", "/usr/bin/python3.12", "/usr/bin/python3.14"]


def _find_system_python() -> str | None:
    for p in SYSTEM_PYTHONS:
        if Path(p).exists():
            return p
    return shutil.which("python3")


class LineServer:
    """Unix-socket server where each connected client gets newline-JSON."""

    def __init__(self, sock_path: Path, on_command=None):
        self.sock_path = sock_path
        self.on_command = on_command  # async fn(dict) -> dict | None
        self._clients: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.sock_path.exists():
            self.sock_path.unlink()
        server = await asyncio.start_unix_server(self._on_client, path=str(self.sock_path))
        os.chmod(self.sock_path, 0o600)
        self._server = server
        log.info("listening on %s", self.sock_path)

    async def _on_client(self, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if self.on_command:
                    try:
                        reply = await self.on_command(msg)
                    except Exception as e:
                        log.error("command handler error: %s", e)
                        reply = {"ok": False, "error": str(e)}
                    if reply is not None:
                        writer.write(json.dumps(reply).encode() + b"\n")
                        await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._clients.discard(writer)
            try:
                writer.close()
            except Exception:
                pass

    def broadcast(self, msg: dict) -> None:
        """Fire-and-forget push to every connected client."""
        if not self._clients:
            return
        data = json.dumps(msg).encode() + b"\n"
        for w in list(self._clients):
            try:
                w.write(data)
            except Exception:
                self._clients.discard(w)


# A control server is just a LineServer that answers commands.
CtlServer = LineServer


async def ctl_client(cmd: str, sock_path: Path | None = None, timeout: float = 3.0,
                     **extra) -> dict:
    """Send one command (plus optional extra fields) to the daemon."""
    import socket as _socket

    sock_path = sock_path or CTL_SOCK
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
        s.sendall(json.dumps({"cmd": cmd, **extra}).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n")[0]
        return json.loads(line) if line else {}
    except OSError as e:
        return {"ok": False, "error": f"daemon not reachable: {e}"}
    finally:
        s.close()


def spawn_hud() -> subprocess.Popen | None:
    """Launch the HUD subprocess with the system python."""
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        log.info("no display — HUD disabled (headless)")
        return None
    py = _find_system_python()
    if py is None:
        log.warning("no system python found for HUD")
        return None
    env = dict(os.environ)
    env["ALPHA_HUD_SOCK"] = str(HUD_SOCK)
    env["ALPHA_CTL_SOCK"] = str(CTL_SOCK)
    log_dir = paths.LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    hud_log = open(log_dir / "hud.log", "ab", buffering=0)  # keep stderr for debugging
    try:
        proc = subprocess.Popen(
            [py, str(HUD_APP)],
            env=env,
            stdout=hud_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.info("HUD subprocess started (pid=%s)", proc.pid)
        return proc
    except OSError as e:
        log.warning("failed to spawn HUD: %s", e)
        return None
