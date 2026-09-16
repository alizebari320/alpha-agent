"""M8: the systemd unit must mirror `memory_max_mb` from the config.

Regression context: `config.toml` documented `memory_max_mb` as "mirrored into
the systemd MemoryMax", but the deployed unit was a static file with 1024M. A
measured warm daemon (1003 MiB) plus the HUD (173 MiB) in one cgroup is
1176 MiB, so the stale limit would have had systemd OOM-kill Alpha mid-request.
"""

from pathlib import Path

from alpha import unit as unit_mod


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
    """With no explicit limit, the value comes from config (2048 by default)."""
    text = unit_mod.render()
    assert "MemoryMax=2048M" in text


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
