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