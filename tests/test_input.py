"""Input backend + safety guard unit tests (no GUI needed)."""

import pytest

from alpha.input.evdev import KEYMAP, char_to_key
from alpha.safety.guard import AbortRequested, SafetyGuard
from alpha.config import SafetyConfig


def test_keymap_covers_essentials():
    for k in ("ctrl", "shift", "alt", "Return", "Tab", "Escape", "F5", "a", "z", "0", "/"):
        assert k in KEYMAP, k


def test_char_to_key_shift():
    assert char_to_key("a") == (30, False)
    assert char_to_key("A") == (30, True)
    assert char_to_key("!") == (2, True)  # shift+1
    assert char_to_key("5") == (6, False)
    assert char_to_key("\n") is None  # use key("Return") instead


def test_allowlist_gating():
    g = SafetyGuard(SafetyConfig())
    assert g.check_action("bash", {"command": "firefox"})[0]
    assert g.check_action("bash", {"command": "nautilus"})[0]
    assert not g.check_action("bash", {"command": "rm -rf ~"})[0]
    assert not g.check_action("bash", {"command": "sudo dnf remove x"})[0]
    assert not g.check_action("bash", {"command": "git push --force origin"})[0]
    assert not g.check_action("bash", {"command": "curl http://x"})[0]  # unlisted


def test_allowlist_denylist_apps():
    cfg = SafetyConfig(denylist=["keepassxc"])
    g = SafetyGuard(cfg)
    assert not g.check_action("click", {"app": "KeePassXC"})[0]
    assert g.check_action("click", {"app": "firefox"})[0]


def test_abort_semantics():
    g = SafetyGuard(SafetyConfig())
    assert not g.abort_requested
    g.check_abort()  # no-op when not aborted
    g.request_abort("test")
    assert g.abort_requested
    with pytest.raises(AbortRequested):
        g.check_abort()
    g.clear_abort()
    g.check_abort()  # fine again


def test_audit_log(tmp_path):
    from alpha.safety.guard import AuditLog

    log = AuditLog(tmp_path / "actions.jsonl")
    log.record({"tool": "click", "x": 1, "y": 2})
    lines = (tmp_path / "actions.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    import json

    entry = json.loads(lines[0])
    assert entry["tool"] == "click" and "ts" in entry
