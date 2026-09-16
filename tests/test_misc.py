"""Provider-agnostic config round-trip and paths tests."""

import tomllib

from alpha import paths
from alpha.config import parse_config
from alpha.credentials import _toml_dump


def test_toml_dump_roundtrip():
    cfg = parse_config({})
    cfg.assistant.name = "jarvis"
    cfg.safety.denylist = ["keepassxc"]
    cfg.llm.fallbacks = []
    dumped = _toml_dump(
        {
            "assistant": {
                "name": cfg.assistant.name,
                "language": cfg.assistant.language,
                "wake_word": {
                    "mode": cfg.assistant.wake_word.mode,
                    "phrase": cfg.assistant.wake_word.phrase,
                    "model": cfg.assistant.wake_word.model,
                    "threshold": cfg.assistant.wake_word.threshold,
                    "refractory_s": cfg.assistant.wake_word.refractory_s,
                },
            }
        }
    )
    # A dict of dicts must emit a [assistant.wake_word] subtable.
    assert "[assistant.wake_word]" in dumped
    parsed = tomllib.loads(dumped)
    assert parsed["assistant"]["name"] == "jarvis"


def test_state_paths_present():
    assert paths.ACTIONS_LOG.name == "actions.jsonl"
    assert paths.STATE_DIR.name == "alpha"
    assert paths.CONFIG_FILE.name == "config.toml"

# ---------------------------------------------------------------------------
# Input backend: key/char resolution (M4)
# ---------------------------------------------------------------------------

def test_char_to_key_space_and_shift():
    from alpha.input import evdev

    assert evdev.char_to_key(" ") == (evdev.KEYMAP["space"], False)
    assert evdev.char_to_key("A") == (evdev.KEYMAP["a"], True)
    assert evdev.char_to_key("7") == (evdev.KEYMAP["7"], False)
    assert evdev.char_to_key("!") == (evdev.KEYMAP["1"], True)
    assert evdev.char_to_key("\u00e9") is None  # non-ASCII: caller must skip


def test_key_code_accepts_names_and_aliases():
    from alpha.input import evdev

    assert evdev.key_code("Return") == 28
    assert evdev.key_code("enter") == 28
    assert evdev.key_code("Escape") == 1
    assert evdev.key_code("esc") == 1
    assert evdev.key_code("super") == evdev.KEY_LEFTMETA
    assert evdev.key_code("F4") == 62
    assert evdev.key_code("Page_Up") == 104
    assert evdev.key_code("pgdn") == 109
    assert evdev.key_code("nonsense-key") is None


def test_uinput_device_requirements():
    """A pointer device must be absolute + INPUT_PROP_POINTER, never a joystick.

    Regression guard for the bug where one mixed device (keys+buttons+REL+ABS)
    was classified by udev as a joystick and ignored by Mutter.
    """
    import pathlib

    src = pathlib.Path("alpha/input/uinput.py").read_text()
    assert "INPUT_PROP_POINTER" in src
    assert "absbits=[evdev.ABS_X, evdev.ABS_Y]" in src
    assert "alpha-agent pointer" in src
    assert "alpha-agent keyboard" in src
    # the old, unreliable approach must be gone: no rel-warp anchoring and no
    # xdotool readback (that implementation silently missed its targets)
    assert "(1 << 15)" not in src
    assert "REL_X" not in src
    assert "getmouselocation" not in src  # no xdotool readback


def test_guard_allows_readonly_verification_commands():
    """The agent must be able to VERIFY its work without a spoken confirmation.

    Regression guard: the first live Firefox run stalled asking permission for
    `wmctrl -l` / `pgrep`, making the loop unusable.
    """
    from alpha.safety.guard import _bash_segments, _segment_is_safe

    allow = ["firefox", "blender", "xdg-open"]
    for cmd in (
        "pgrep -a firefox | head -5",
        'wmctrl -l 2>/dev/null || xdotool search --name "" getwindowname %@',
        "xdotool getactivewindow getwindowname",
        "gsettings get org.gnome.desktop.interface enable-hot-corners",
        "cat /etc/hostname && date",
        "firefox",
    ):
        segs = _bash_segments(cmd)
        assert segs and all(_segment_is_safe(s, allow) for s in segs), cmd

    # ...but nothing that writes, injects input, or destroys may slip through
    for cmd in (
        "ls; rm -rf /home/donk",
        "xdotool key ctrl+l",
        "gsettings set org.gnome.desktop.peripherals.mouse accel-profile flat",
        "echo hi > /etc/passwd",
        "python3 -c 'import shutil; shutil.rmtree(\"/home/donk\")'",
    ):
        segs = _bash_segments(cmd)
        assert not all(_segment_is_safe(s, allow) for s in segs), cmd


def test_guard_destructive_regex_still_catches_dangerous_commands():
    from alpha.safety.guard import DESTRUCTIVE_RE

    for cmd in ("sudo rm -rf /", "rm file.txt", "mkfs.ext4 /dev/sda1",
                "shutdown -h now", "git push --force origin main"):
        assert DESTRUCTIVE_RE.search(cmd), cmd
    for cmd in ("pgrep firefox", "ls -la", "wmctrl -l"):
        assert not DESTRUCTIVE_RE.search(cmd), cmd


def test_gui_launches_do_not_block_the_loop():
    """`bash firefox` must return immediately.

    Regression guard: a foreground `firefox` blocked the tool for its full 30s
    timeout and was then misreported to the planner as a failure.
    """
    import alpha.brain.loop as loop

    assert "firefox" in loop.GUI_LAUNCHERS

    class FakePopen:
        def __init__(self, *a, **kw):
            self.pid = 4242
            self.returncode = None
            self.kw = kw

        def poll(self):
            return None

    real_popen, real_run, real_sleep = loop.subprocess.Popen, loop.subprocess.run, loop.time.sleep
    launched = {}

    def fake_popen(cmd, **kw):
        launched["cmd"] = cmd
        launched["kw"] = kw
        return FakePopen()

    def fake_run(*a, **kw):
        raise AssertionError("GUI app must not go through the blocking path")

    loop.subprocess.Popen = fake_popen
    loop.subprocess.run = fake_run
    loop.time.sleep = lambda s: None
    try:
        out = loop._run_bash("firefox --new-tab")
    finally:
        loop.subprocess.Popen, loop.subprocess.run, loop.time.sleep = real_popen, real_run, real_sleep

    assert "launched in background" in out
    assert launched["kw"].get("start_new_session") is True  # survives the daemon
    assert launched["cmd"] == "firefox --new-tab"


def test_prompt_forbids_relaunching_a_running_app():
    from alpha.brain.prompts import system_prompt

    p = system_prompt("alpha", (1920, 1080), None,
                      {"x": 0, "y": 0, "w": 1920, "h": 1080}, "wayland", True)
    assert "already running" in p.lower()
    # blind + no screen share must be stated explicitly
    blind = system_prompt("alpha", (1920, 1080), None,
                          {"x": 0, "y": 0, "w": 1920, "h": 1080}, "wayland", False,
                          screen_share_ok=False)
    assert "UNAVAILABLE" in blind
    assert "Share" in blind


def test_provider_error_messages_are_actionable():
    """Quota / unavailable-model errors must be understandable, not generic."""
    from alpha.brain.providers.openai_compatible import _is_html

    class R:
        def __init__(self, ct, text):
            self.headers = {"content-type": ct}
            self.text = text

    assert _is_html(R("text/html", "<!doctypehtml><title>405</title>"))
    assert not _is_html(R("application/json", '{"choices": []}'))
