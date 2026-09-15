"""Ollama provider — fully local, optional (§3).

Uses the OpenAI-compatible endpoint Ollama exposes at /v1, so this is a thin
specialization of OpenAICompatibleProvider with a local default base URL and
keyless auth.
"""

from __future__ import annotations

from ...config import LLMRole
from .openai_compatible import OpenAICompatibleProvider


class OllamaProvider(OpenAICompatibleProvider):
    def __init__(self, model: str = "qwen3:8b", base_url: str = "http://127.0.0.1:11434/v1"):
        role = LLMRole(
            provider="ollama",
            model=model,
            base_url=base_url,
            user_agent="alpha-agent",
            api_key_keyring_user="ollama",  # ollama needs no key
        )
        super().__init__(role)
        self.key = "ollama"  # ollama ignores auth but wants a non-empty header

    def supports_vision(self) -> bool:
        return "llava" in self.role.model or "vl" in self.role.model
