"""M8: the systemd unit must mirror `memory_max_mb` from the config.

Regression context: `config.toml` documented `memory_max_mb` as "mirrored into
the systemd MemoryMax", but the deployed unit was a static file with 1024M. A
measured warm daemon (1003 MiB) plus the HUD (173 MiB) in one cgroup is
1176 MiB, so the stale limit would have had systemd OOM-kill Alpha mid-request.
"""

from pathlib import Path

from alpha import unit as unit_mod


def test_template_ships_inside_the_package():
    """The unit template must be importable as a package resource.

    Regression: reading `scripts/alpha.service` from the repo root meant
    `alpha install-service` crashed with FileNotFoundError on any wheel install.
    """
    assert unit_mod.PACKAGED_TEMPLATE.is_file(), (
        "alpha/templates/alpha.service is missing from the package; "
        "alpha install-service would fail on a pip/uv install"
    )
    assert unit_mod.template_path() == unit_mod.PACKAGED_TEMPLATE
    assert "[Service]" in unit_mod.template_path().read_text()


def test_render_mirrors_memory_max(tmp_path):
    text = unit_mod.render(exec_start="/opt/alpha/venv/bin/alpha",
                           memory_max_mb=1536, repo_dir=Path("/opt/alpha"))
    assert "MemoryMax=1536M" in text
    assert "ExecStart=/opt/alpha/venv/bin/alpha" in text
    assert "WorkingDirectory=/opt/alpha" in text
    # the template default must never leak through when a limit is supplied
    assert "MemoryMax=1024M" not in text
    # and it must still be a valid user unit
    assert "[Service]" in text and "WantedBy=default.target" in text


def test_render_uses_config_default():
    """With no explicit limit, the value comes from config (2048 by default).

    The config is passed in, never read from disk: reading the developer's real
    ~/.config/alpha/config.toml made this test fail on a clean machine (there is
    no config there) and pass or fail depending on whose machine ran it.
    """
    from alpha.config import Config

    text = unit_mod.render(config=Config())
    assert "MemoryMax=2048M" in text


def test_render_honours_a_custom_config_limit():
    """The mirror must be real: change the config, change the unit."""
    from alpha.config import Config, ResourceConfig

    cfg = Config()
    cfg.resources = ResourceConfig(memory_max_mb=3072)
    assert "MemoryMax=3072M" in unit_mod.render(config=cfg)


def test_render_needs_no_config_file_when_both_values_are_given(tmp_path):
    """An explicit budget must not touch the disk at all."""
    text = unit_mod.render(exec_start="/x/alpha", memory_max_mb=512,
                           repo_dir=tmp_path)
    assert "MemoryMax=512M" in text


def test_deployed_memory_max_reads_the_unit(tmp_path, monkeypatch):
    p = tmp_path / "alpha.service"
    p.write_text("[Service]\nMemoryMax=3072M\n")
    monkeypatch.setattr(unit_mod, "unit_path", lambda: p)
    assert unit_mod.deployed_memory_max() == 3072


def test_deployed_memory_max_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(unit_mod, "unit_path", lambda: tmp_path / "nope.service")
    assert unit_mod.deployed_memory_max() is None


def test_drift_detects_stale_unit(tmp_path, monkeypatch):
    """The exact bug: config says 2048, the deployed unit still says 1024."""
    from alpha import config as config_mod
    from alpha.config import Config, ResourceConfig

    p = tmp_path / "alpha.service"
    p.write_text("[Service]\nMemoryMax=1024M\n")
    monkeypatch.setattr(unit_mod, "unit_path", lambda: p)

    cfg = Config()
    cfg.resources = ResourceConfig(memory_max_mb=2048)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)

    cfg_limit, deployed = unit_mod.drift()
    assert (cfg_limit, deployed) == (2048, 1024)
    assert cfg_limit != deployed  # what doctor reports as drift


def test_install_service_exits_nonzero_when_it_cannot_install(tmp_path, monkeypatch, capsys):
    """A failed install must not report success to scripts/CI.

    Regression: main() printed "could not install unit: ..." and still returned
    0, so `alpha install-service && systemctl --user restart alpha` looked fine
    while nothing had been written.
    """
    from alpha import unit as unit_mod

    def boom(*a, **k):
        raise unit_mod.FileNotFoundError("no config at /nonexistent")

    monkeypatch.setattr(unit_mod, "render", boom)
    monkeypatch.setattr(unit_mod, "unit_path", lambda: tmp_path / "alpha.service")
    monkeypatch.setattr(unit_mod.paths, "ensure_dirs", lambda: None)
    assert unit_mod.main() == 1
    assert "could not" in capsys.readouterr().err


class _FakeTranscriber:
    """Stands in for whisper-tiny so wake matching is testable without audio."""

    def __init__(self, text):
        self._text = text

    def transcribe(self, pcm):
        return self._text


def _wake_for(heard: str, phrases=("hey alpha", "hi alpha")):
    from alpha.wake import KWSWake

    return KWSWake(list(phrases), transcriber=_FakeTranscriber(heard))


def test_wake_accepts_every_configured_phrase():
    """'hi alpha' must wake Alpha — it is the phrase users reach for; whisper
    tiny transcribes it as 'high alpha.', which the old single-phrase matcher
    rejected outright."""
    import numpy as np

    silence = np.zeros(1600, dtype=np.int16)
    for heard in ("Hey, Alfa.", "high alpha.", "Hi, Alpha what time is it?",
                  "Okay Alpha", "Hey, Alpha, how are you?"):
        assert _wake_for(heard).feed_utterance(silence) is True, heard


def test_wake_accepts_mangled_greetings():
    """Regression: real whisper-tiny output for a spoken wake word mangles the
    greeting half ('hi alpha' -> 'Ha, y'all for.' on a noisy mic, 'high alpha.'
    on a clean one). Neither the exact phrase nor per-word matching fired on the
    mangled form, so Alpha never woke. The greeting-variant rule accepts any
    common opening once the NAME is clear; the sliding window handles the
    'high alpha.' form.
    """
    import numpy as np

    silence = np.zeros(1600, dtype=np.int16)
    # observed transcriptions of spoken greetings + the configured phrases
    for heard in ("high alpha.", "High, Alpha.", "hello alfa", "okay alfa",
                  "hi alfa", "Hey Alfa!", "hello alpha"):
        assert _wake_for(heard).feed_utterance(silence) is True, heard
    # ... but the name still has to be there, so these stay false
    for heard in ("Hi, Siri.", "Hey, computer.", "high there.", "hi", "okay"):
        assert _wake_for(heard).feed_utterance(silence) is False, heard


def test_wake_ignores_ordinary_speech():
    """Regression: background speech woke Alpha for real.

    The journal showed `wake! kws fuzzy 'hey alfa' ~ 'half' (0.67)` while a
    video was playing, so a single stray word could open the microphone and
    start a 15 s recording. Short words must also match almost exactly, so a
    transcript with no real phrase in it must never wake.
    """
    import numpy as np

    silence = np.zeros(1600, dtype=np.int16)
    for heard in ("and we're now able to put half of this on this.",
                  "That's it for now, see you in the next video.",
                  "the doubt to be able to see me elsewhere.",
                  "I didn't laugh, so I had to.", "Let's go.", "what time is it"):
        assert _wake_for(heard).feed_utterance(silence) is False, heard


def test_wake_config_exposes_a_phrase_list():
    from alpha.config import WakeWordConfig

    assert WakeWordConfig().wake_phrases() == ["hey alpha"]
    assert WakeWordConfig(phrase="yo alpha").wake_phrases() == ["yo alpha"]
    assert WakeWordConfig(phrases=["hey alpha", "hi alpha"]).wake_phrases() == [
        "hey alpha", "hi alpha"]
    # a blank list must not leave the matcher with nothing to match
    assert WakeWordConfig(phrases=[]).wake_phrases() == ["hey alpha"]
