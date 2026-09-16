"""Configuration tests."""

import tomllib

import pytest

from alpha.config import ConfigError, parse_config
from alpha.wake import BUNDLED_WAKEWORDS


def test_parse_defaults():
    cfg = parse_config({})
    assert cfg.assistant.name == "alpha"
    assert cfg.llm.planner.model == "glm-5.3"
    assert cfg.llm.max_steps == 25
    assert cfg.vision.max_width == 1280


def test_parse_full_config():
    data = tomllib.loads(
        """
        [assistant]
        name = "jarvis"
        language = "ar"

        [assistant.wake_word]
        model = "hey_jarvis"
        mode = "pretrained"
        phrase = "hey jarvis"
        threshold = 0.7
        refractory_s = 1.0

        [llm.planner]
        provider = "agentrouter"
        model = "glm-5.3"
        base_url = "https://agentrouter.org/v1"
        user_agent = "opencode/1.18.30"
        api_key_keyring_user = "agentrouter"

        [safety]
        denylist = ["keepassxc", "banking"]
        bash_allowlist = ["firefox"]

        [vision]
        enabled = "on"
        max_width = 1024

        [resources]
        idle_unload_s = 60
        """
    )
    cfg = parse_config(data)
    assert cfg.assistant.name == "jarvis"
    assert cfg.assistant.wake_word.threshold == 0.7
    assert cfg.safety.denylist == ["keepassxc", "banking"]
    assert cfg.safety.bash_allowlist == ["firefox"]
    assert cfg.vision.max_width == 1024
    assert cfg.llm.planner.provider == "agentrouter"


def test_unknown_pretrained_wake_word_rejected():
    data = {"assistant": {"wake_word": {"mode": "pretrained", "model": "hey_nonexistent"}}}
    with pytest.raises(ConfigError):
        parse_config(data)


def test_kws_mode_allows_any_phrase():
    data = {"assistant": {"wake_word": {"mode": "kws", "phrase": "hey alpha"}}}
    cfg = parse_config(data)
    assert cfg.assistant.wake_word.phrase == "hey alpha"
    assert cfg.assistant.wake_word.mode == "kws"


def test_default_mode_is_kws():
    # Spec §8 Tier 3: works with ANY name instantly out of the box.
    cfg = parse_config({})
    assert cfg.assistant.wake_word.mode == "kws"


def test_invalid_vision_mode():
    data = {"vision": {"enabled": "sometimes"}}
    with pytest.raises(ConfigError):
        parse_config(data)


def test_invalid_stt_model():
    data = {"stt": {"model": "gigantic"}}
    with pytest.raises(ConfigError):
        parse_config(data)


def test_negative_spend_cap_rejected():
    data = {"llm": {"daily_spend_cap_usd": -1.0}}
    with pytest.raises(ConfigError):
        parse_config(data)


def test_bundled_wakewords_present():
    # Tier 1 needs at least a few pretrained names so the wizard has options.
    assert len(BUNDLED_WAKEWORDS) >= 4
    # These are the models openwakeword actually ships (verified live).
    assert "hey_jarvis" in BUNDLED_WAKEWORDS


def test_unknown_config_key_is_warned_about_not_ignored():
    """A typo must not be a silent no-op.

    Regression: docs told users to set `wake_word.min_score`, which is not a
    field — the parser dropped it, threshold stayed 0.6, and the only symptom
    was "tuning does nothing". The warning names the key and lists the valid
    ones so the fix is obvious.
    """
    from alpha.config import parse_config

    cfg = parse_config({
        "assistant": {"wake_word": {"mode": "kws", "min_score": 0.9,
                                    "phrase": "hey alpha"}},
    })
    assert cfg.assistant.wake_word.threshold == 0.6  # unchanged, as before
    assert len(cfg.warnings) == 1
    warning = cfg.warnings[0]
    assert "min_score" in warning
    assert "assistant.wake_word" in warning
    assert "threshold" in warning          # the valid keys are listed
    assert "did you mean" in warning       # and a suggestion is offered


def test_valid_config_produces_no_warnings():
    from alpha.config import parse_config

    cfg = parse_config({
        "assistant": {"name": "alpha", "language": "en",
                      "wake_word": {"mode": "kws", "phrase": "hey alpha",
                                    "threshold": 0.6, "min_rms": 200.0}},
        "stt": {"model": "auto", "language": "auto", "compute_type": "int8"},
    })
    assert cfg.warnings == []
    assert cfg.assistant.wake_word.min_rms == 200.0
