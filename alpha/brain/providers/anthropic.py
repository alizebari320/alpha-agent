"""Anthropic-native provider (optional fast path).

Supports Anthropic's /v1/messages API — INCLUDING the native `computer` tool
schema if you want it — but normalizes to Alpha's own tools either way
(AgentRouter/GLM don't speak the Anthropic schema, which is why the OpenAI-
compatible provider is the default).
"""

from __future__ import annotations

import json
import logging
import time

import httpx

from ...config import LLMRole
from ...credentials import get_key
from .openai_compatible import LLMResponse, ToolCall

log = logging.getLogger(__name__)

# Map Alpha's neutral tools -> Anthropic's tool schema (function tools only;
# the native computer_2025xxxx fast path can be added behind a config flag).


def to_anthropic_tools(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools:
        fn = t["function"]
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


class AnthropicProvider:
    def __init__(self, role: LLMRole):
        self.role = role
        self.base_url = role.base_url.rstrip("/")
        self.key = get_key(role.api_key_keyring_user) or ""

    def send(self, messages: list[dict], tools: list[dict] | None = None,
             max_tokens: int = 2048, retries: int = 2) -> LLMResponse:
        headers = {
            "x-api-key": self.key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            # Some gateways whitelist client UAs (AgentRouter does).
            "User-Agent": self.role.user_agent,
        }
        # translate messages: system prompt separate; images -> content blocks
        system = ""
        amsgs = []
        for m in messages:
            content = m.get("content")
            if m.get("role") == "system":
                system = content if isinstance(content, str) else system
                continue
            if m.get("role") == "tool":
                amsgs.append({"role": "user", "content": [
                    {"type": "text", "text": f"tool {m.get('tool_call_id')} result: "
                                              + str(content)[:400]}]})
                continue
            if isinstance(content, list):
                blocks = []
                for b in content:
                    if b.get("type") == "text":
                        blocks.append({"type": "text", "text": b.get("text", "")})
                    elif b.get("type") == "image_url":
                        url = b["image_url"]["url"]
                        media, b64 = url.split(",", 1)
                        fmt = media.split("/")[1].split(";")[0]
                        blocks.append({"type": "image",
                                       "source": {"type": "base64", "media_type": f"image/{fmt}",
                                                  "data": b64}})
                amsgs.append({"role": "user", "content": blocks})
            elif m.get("role") == "assistant":
                calls = m.get("tool_calls") or []
                if calls:
                    blocks = []
                    for c in calls:
                        fn = c["function"]
                        blocks.append({"type": "tool_use", "id": c.get("id") or "t",
                                       "name": fn["name"],
                                       "input": json.loads(fn.get("arguments") or "{}")})
                    amsgs.append({"role": "assistant", "content": blocks})
                else:
                    amsgs.append({"role": "assistant", "content": str(content or "")[:2000]})
            else:
                amsgs.append({"role": "user", "content": str(content or "")[:2000]})

        body = {"model": self.role.model, "max_tokens": max_tokens, "messages": amsgs}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = to_anthropic_tools(tools)
        url = f"{self.base_url}/v1/messages"

        last_err = None
        for attempt in range(retries + 1):
            try:
                r = httpx.post(url, json=body, headers=headers, timeout=120.0)
                if r.status_code == 200:
                    return self._parse(r.json())
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = f"HTTP {r.status_code}"
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"anthropic HTTP {r.status_code}: {r.text[:200]}")
            except httpx.HTTPError as e:
                last_err = str(e)
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"anthropic provider failed: {last_err}")

    def _parse(self, data: dict) -> LLMResponse:
        text = ""
        calls = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(id=block.get("id", ""), name=block.get("name", ""),
                                      arguments=block.get("input", {})))
        usage = data.get("usage", {})
        return LLMResponse(text=text, tool_calls=calls,
                           usage={"prompt_tokens": usage.get("input_tokens", 0),
                                  "completion_tokens": usage.get("output_tokens", 0)},
                           finish_reason=data.get("stop_reason", ""))

    def supports_vision(self) -> bool:
        # Claude models accept images
        return True
