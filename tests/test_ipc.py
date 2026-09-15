"""IPC layer tests (LineServer / ctl protocol) — pure asyncio, no GUI."""

import asyncio
import json

import pytest

from alpha.ipc import LineServer


@pytest.mark.asyncio
async def test_lineserver_broadcast_and_command(tmp_path):
    sock = tmp_path / "test.sock"
    seen_commands = []

    async def on_cmd(msg):
        seen_commands.append(msg)
        return {"ok": True, "echo": msg.get("cmd")}

    server = LineServer(sock, on_command=on_cmd)
    await server.start()

    reader, writer = await asyncio.open_unix_connection(str(sock))
    writer.write(json.dumps({"cmd": "hello"}).encode() + b"\n")
    await writer.drain()

    reply = json.loads(await asyncio.wait_for(reader.readline(), 2.0))
    assert reply == {"ok": True, "echo": "hello"}
    assert seen_commands == [{"cmd": "hello"}]

    # broadcast reaches the connected client
    server.broadcast({"type": "state", "state": "listening", "text": "hi"})
    msg = json.loads(await asyncio.wait_for(reader.readline(), 2.0))
    assert msg["type"] == "state"
    assert msg["state"] == "listening"

    writer.close()
    server._server.close()


@pytest.mark.asyncio
async def test_lineserver_no_clients_broadcast_ok(tmp_path):
    """Broadcast with zero connected clients must not raise (HUD may be down)."""
    server = LineServer(tmp_path / "x.sock")
    await server.start()
    server.broadcast({"type": "mute", "muted": True})  # must not raise
    server._server.close()


@pytest.mark.asyncio
async def test_bad_json_ignored(tmp_path):
    server = LineServer(tmp_path / "y.sock")
    await server.start()
    reader, writer = await asyncio.open_unix_connection(str(server.sock_path))
    writer.write(b"not json\n")
    await writer.drain()
    await asyncio.sleep(0.1)
    assert not writer.is_closing()
    writer.close()
    server._server.close()
