"""Provider abstraction (M6).

An LLMProvider sends messages + tools and returns normalized tool calls.
AgentRouter/GLM are OpenAI-compatible and do NOT support Anthropic's native
``computer`` tool schema, so Alpha defines its own provider-neutral JSON tools
and maps every provider onto them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = ""


@dataclass
class LLMMessage:
    role: str
    content: str | list  # str, or list of content blocks (text + image)
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMProvider(ABC):
    @abstractmethod
    def send(self, messages: list[LLMMessage], tools: list[dict]) -> list[LLMMessage]:
        """Return assistant messages containing text and/or normalized ToolCalls."""

    @abstractmethod
    def supports_vision(self) -> bool:
        """Whether the configured model accepts image input."""


# M1 provides only the probe used by `alpha doctor`; the full provider lands in
# M6. See alpha/doctor.py for the OpenAI-compatible reachability check.