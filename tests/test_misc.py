"""Provider-agnostic config round-trip and paths tests."""

import tomllib

import pytest

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


def test_resolve_pretrained_model_accepts_trained_models(tmp_path, monkeypatch):
    """A trained wake word must be usable, not just installable.

    Regression: config validation let a custom model through when
    ~/.local/share/alpha/wakewords/<name>.onnx existed, but PretrainedWake then
    raised ValueError unless the name was in BUNDLED_WAKEWORDS — so the
    documented train -> --install -> use flow passed validation and crashed the
    daemon at startup.
    """
    from alpha import wake as wake_mod

    monkeypatch.setattr(wake_mod.paths, "WAKEWORD_DIR", tmp_path)

    # bundled names pass through untouched (openWakeWord resolves them)
    for name in wake_mod.BUNDLED_WAKEWORDS:
        assert wake_mod.resolve_pretrained_model(name) == name

    # a trained model resolves to its file path
    trained = tmp_path / "hey_alpha.onnx"
    trained.write_bytes(b"not a real model")
    assert wake_mod.resolve_pretrained_model("hey_alpha") == str(trained)

    # a .tflite-only model is found too
    (tmp_path / "hey_beta.tflite").write_bytes(b"x")
    assert wake_mod.resolve_pretrained_model("hey_beta").endswith("hey_beta.tflite")

    # unknown names still fail, with an actionable message
    with pytest.raises(ValueError) as e:
        wake_mod.resolve_pretrained_model("nope")
    msg = str(e.value)
    assert "nope" in msg and "train-wake-word.py" in msg and "kws" in msg


def test_wake_gate_needs_both_loudness_and_speech():
    """Regression context: every window the gate lets through costs a whisper
    decode at a fixed ~0.65 s of CPU (whisper pads to a 30 s mel chunk), so the
    gate is the main throttle on background CPU in a noisy room.
    """
    from alpha.listen import KWS_MIN_SPEECH_RATIO, wake_gate_opens

    floor = 120.0
    assert KWS_MIN_SPEECH_RATIO == 0.15

    # loud enough and speech-like -> decode
    assert wake_gate_opens(0.5, 2000.0, floor)
    # loud but the VAD says it is not speech (a door slam, music) -> skip
    assert not wake_gate_opens(0.05, 9000.0, floor)
    # speech-like but only room tone -> skip
    assert not wake_gate_opens(0.9, 40.0, floor)
    # exactly on both boundaries counts as open (>=, not >)
    assert wake_gate_opens(KWS_MIN_SPEECH_RATIO, floor, floor)
    # a higher floor (noisy room) closes it
    assert not wake_gate_opens(0.9, 400.0, min_rms=600.0)


def test_rms_matches_known_signals():
    import numpy as np

    from alpha.listen import rms

    assert rms(np.zeros(0, dtype=np.int16)) == 0.0
    assert rms(np.zeros(16000, dtype=np.int16)) == 0.0
    assert rms(np.full(1000, 32767, dtype=np.int16)) == 32767.0
    # a full-scale square wave of half 1s and half -1s still has rms 1.0*scale
    sq = np.array([1000, -1000] * 500, dtype=np.int16)
    assert abs(rms(sq) - 1000.0) < 1e-6
    # int16 units, NOT normalised to 0..1 (a float conversion bug would give 1.0)
    assert rms(np.full(1000, 1000, dtype=np.int16)) > 100.0


def test_min_rms_is_configurable_and_validated():
    from alpha.config import parse_config

    assert parse_config({}).assistant.wake_word.min_rms == 300.0
    cfg = parse_config({"assistant": {"wake_word": {"min_rms": 450}}})
    assert cfg.assistant.wake_word.min_rms == 450.0
    assert isinstance(cfg.assistant.wake_word.min_rms, float)


def test_trained_wakeword_installs_where_config_looks(tmp_path, monkeypatch):
    """The install destination and the config lookup must be the same directory.

    Regression: --install wrote to ~/.local/share/alpha/models/ while config
    validation (and the loader) looked in ~/.local/share/alpha/wakewords/, so
    following the documented flow produced
    "unknown pretrained wake word model 'hey_alpha'" on the next start.
    """
    from alpha import paths

    assert paths.WAKEWORD_DIR.name == "wakewords"
    assert paths.MODEL_DIR.name == "models"
    assert paths.WAKEWORD_DIR != paths.MODEL_DIR


    from alpha import wake as wake_mod

    monkeypatch.setattr(wake_mod.paths, "WAKEWORD_DIR", tmp_path)
    installed = tmp_path / "hey_alpha.onnx"
    installed.write_bytes(b"stub")
    # the loader must find exactly what an install into WAKEWORD_DIR produces
    assert wake_mod.resolve_pretrained_model("hey_alpha") == str(installed)


def test_noise_floor_adapts_to_room_tone_but_never_below_the_absolute_minimum():
    """The wake gate must not be fooled by a fixed floor, nor chase itself down.

    Measured motivation (docs/PERFORMANCE.md): room tone 280-955 with speech
    ~2000, and silero called noise "speech" often enough that the wake path paid
    ~0.65 s of CPU per wasted decode, ~31 times in 6 minutes.
    """
    from alpha.listen import NoiseFloor, wake_gate_opens

    n = NoiseFloor(floor_min=300.0)
    assert n.room_tone == 0.0            # nothing observed yet
    assert n.threshold() == 300.0        # so the configured floor applies

    # a silent room must NOT normalise its way down to nothing
    for _ in range(50):
        n.observe(10.0)
    assert n.threshold() == 300.0

    # intermittent speech over a noisy floor: the low percentile must ignore the
    # loud windows, so the floor tracks room tone (400), not speech (3000)
    n = NoiseFloor(floor_min=300.0)
    for i in range(100):
        n.observe(3000.0 if i % 10 == 0 else 400.0)
    assert 380 <= n.room_tone <= 420
    floor = n.threshold()
    assert 760 <= floor <= 840          # 2x room tone

    # consequences: the noise is rejected, a normal voice still gets through
    assert not wake_gate_opens(0.5, 400.0, floor)
    assert wake_gate_opens(0.5, 900.0, floor)
    assert wake_gate_opens(0.5, 2500.0, floor)
