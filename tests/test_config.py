"""Configuration tests."""

import tomllib

import pytest

from alpha import paths
from alpha.config import BUNDLED_WAKEWORDS, Config, ConfigError, parse_config


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
        model = "hey_alpha"
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


def test_unknown_wake_word_rejected():
    data = {"assistant": {"wake_word": {"model": "hey_nonexistent"}}}
    with pytest.raises(ConfigError):
        parse_config(data)


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