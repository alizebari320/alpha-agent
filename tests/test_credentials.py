"""Credential discovery + redaction tests (no live network, no live keyring)."""

import json

import pytest

from alpha.credentials import discover_opencode
from alpha.log import redact


@pytest.fixture
def opencode_files(tmp_path):
    config = tmp_path / "opencode.json"
    auth = tmp_path / "auth.json"

    config_data = {
        "model": "agentrouter-openai/glm-5.3",
        "provider": {
            "agentrouter-openai": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "baseURL": "https://agentrouter.org/v1",
                    "apiKey": "sk-CURRENT_FROM_CONFIG",
                },
                "models": {"glm-5.3": {"name": "GLM 5.3"}, "deepseek-v4-flash": {}},
            },
            "agentrouter": {
                "npm": "@ai-sdk/anthropic",
                "options": {"baseURL": "https://agentrouter.org", "apiKey": "sk-ANTHROPIC"},
                "models": {"claude-sonnet-4-5": {}},
            },
            "no-base": {
                "npm": "@ai-sdk/openai-compatible",
                # no baseURL -> should be skipped
                "options": {"apiKey": "sk-NO_BASE"},
            },
        },
    }
    auth_data = {
        # auth store is often STALE; literal apiKey in config must win
        "agentrouter-openai": {"type": "api", "key": "sk-STALE_FROM_AUTH"},
    }
    config.write_text(json.dumps(config_data))
    auth.write_text(json.dumps(auth_data))
    return config, auth


def test_discover_prefers_agentrouter_openai(opencode_files):
    config, auth = opencode_files
    result = discover_opencode(config_path=config, auth_path=auth)

    provider_ids = {p.id for p in result.providers}
    assert "agentrouter-openai" in provider_ids
    assert "agentrouter" in provider_ids
    assert "no-base" not in provider_ids  # skipped, no baseURL

    # literal config apiKey wins over the (often stale) auth store key
    openai = next(p for p in result.providers if p.id == "agentrouter-openai")
    assert openai.api_key == "sk-CURRENT_FROM_CONFIG"
    assert openai.base_url == "https://agentrouter.org/v1"
    assert openai.model == "glm-5.3"  # top-level default model resolved

    # preferred provider chosen as default
    assert result.chosen is not None
    assert result.chosen.id == "agentrouter-openai"


def test_discover_empty(tmp_path):
    result = discover_opencode(config_path=tmp_path / "none.json", auth_path=tmp_path / "none2.json")
    assert result.providers == []
    assert result.chosen is None


def test_env_var_key_resolution(tmp_path, monkeypatch):
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps(
            {
                "provider": {
                    "p": {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {"baseURL": "https://x/v1", "apiKey": "{env:MY_KEY}"},
                        "models": {"m": {}},
                    }
                }
            }
        )
    )
    monkeypatch.setenv("MY_KEY", "sk-FROM_ENV")
    result = discover_opencode(config_path=config, auth_path=tmp_path / "none.json")
    assert result.providers[0].api_key == "sk-FROM_ENV"



def test_redact_keys():
    assert redact("api key: sk-PREFIX123456789") == "api key: <redacted>"
    assert "sk-PREFIX123456789" not in redact("Authorization: Bearer sk-PREFIX123456789")
    # normal text untouched
    assert redact("hello world") == "hello world"
