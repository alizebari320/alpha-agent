"""LLM provider abstraction: OpenAI-compatible first (AgentRouter/GLM).

Every provider normalizes to: messages in, assistant message(s) out with
text and/or ToolCall objects. Vision support is probed at runtime when
vision.enabled = 'auto' (a model that rejects image blocks falls back to
text-only grounding — AT-SPI + OmniParser text, no screenshot).
"""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from ... import paths
from ...config import LLMRole
from ...credentials import get_key

log = logging.getLogger(__name__)


def _is_html(r) -> bool:
    """True when an error response is an HTML page, not API JSON."""
    ctype = (r.headers.get("content-type") or "").lower()
    body = (r.text or "")[:200].lstrip().lower()
    return "text/html" in ctype or body.startswith("<!doctype") or body.startswith("<html")


@dataclass
class ToolCall:
    id: str = ""
    name: str = ""
    arguments: dict = field(default_factory=dict)
    arguments_raw: str = ""


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)  # prompt/completion tokens
    finish_reason: str = ""


class LLMProvider(ABC):
    @abstractmethod
    def send(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        """messages: OpenAI-style dicts (role/content/tool_calls/tool_call_id)."""

    def supports_vision(self) -> bool:  # overridden
        return False


class OpenAICompatibleProvider(LLMProvider):
    """Works for AgentRouter, GLM, OpenRouter, local vLLM — anything OpenAI-shaped."""

    def __init__(self, role: LLMRole):
        self.role = role
        self.base_url = role.base_url.rstrip("/")
        self.key = get_key(role.api_key_keyring_user) or ""
        self._vision: bool | None = None
        if not self.key:
            log.warning("no API key in keyring for %r", role.api_key_keyring_user)

    # ------------------------------------------------------------------ send

    def send(self, messages: list[dict], tools: list[dict] | None = None,
             max_tokens: int = 2048, retries: int = 2) -> LLMResponse:
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            # Some gateways whitelist clients by User-Agent (AgentRouter does).
            "User-Agent": self.role.user_agent,
        }
        body: dict = {
            "model": self.role.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        url = f"{self.base_url}/chat/completions"

        last_err = None
        for attempt in range(retries + 1):
            try:
                r = httpx.post(url, json=body, headers=headers, timeout=120.0)
                if r.status_code == 200:
                    return self._parse(r.json())
                # 429/5xx: retry with backoff
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = self._describe_error(r)
                    time.sleep(1.5 * (attempt + 1))
                    continue
                # Gateways in front of the API occasionally answer with an
                # HTML error page (observed: HTTP 405 from a CDN edge). Those
                # are transient infrastructure noise, not a bad request, so
                # retry them instead of failing the user's request.
                if _is_html(r):
                    # WAF-ish rejections are content-dependent; keep the body
                    # around so the user can see exactly what was refused.
                    if os.environ.get("ALPHA_DEBUG_PAYLOAD"):
                        try:
                            paths.ensure_dirs()
                            dump = paths.STATE_DIR / "rejected-request.json"
                            dump.write_text(json.dumps({"url": url, "headers": {
                                k: ("***" if k == "Authorization" else v) for k, v in headers.items()},
                                "body": body}, indent=2)[:4_000_000])
                            log.warning("rejected request dumped to %s", dump)
                        except Exception:  # noqa: BLE001
                            pass
                if _is_html(r) and attempt < retries:
                    last_err = f"HTTP {r.status_code} (gateway HTML page)"
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(self._describe_error(r))
            except httpx.HTTPError as e:
                last_err = str(e)
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"provider failed after retries: {last_err}")

    def _describe_error(self, r: "httpx.Response") -> str:
        """Turn a provider error into something a human can act on."""
        text = (r.text or "")[:300]
        if "no available channel" in text or "无可用渠道" in text:
            models = self.available_models()
            hint = f" Available models: {', '.join(models)}" if models else ""
            return (f"model {self.role.model!r} is not available on this account "
                    f"(HTTP {r.status_code}).{hint}")
        if "quota" in text.lower():
            return f"provider quota exhausted: {text}"
        if _is_html(r):
            return f"HTTP {r.status_code}: gateway returned an HTML error page"
        return f"provider HTTP {r.status_code}: {text}"

    def available_models(self) -> list[str]:
        """Model ids this account may use (empty list if unavailable)."""
        try:
            r = httpx.get(f"{self.base_url}/models", timeout=15.0, headers={
                "Authorization": f"Bearer {self.key}",
                "User-Agent": self.role.user_agent,
            })
            if r.status_code == 200:
                return [m.get("id", "") for m in (r.json().get("data") or []) if m.get("id")]
        except httpx.HTTPError:
            pass
        return []

    def _parse(self, data: dict) -> LLMResponse:
        msg = (data.get("choices") or [{}])[0].get("message", {})
        text = msg.get("content") or ""
        # reasoning models (GLM) put chain-of-thought in reasoning_content;
        # NEVER spoken or shown — drop it.
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function", {})
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=c.get("id") or "", name=fn.get("name") or "",
                                  arguments=args, arguments_raw=args_raw))
        return LLMResponse(text=text, tool_calls=calls,
                           usage=data.get("usage") or {}, finish_reason=data.get("choices", [{}])[0].get("finish_reason", ""))

    # ---------------------------------------------------------------- vision

    def supports_vision(self) -> bool:
        """Probe once: send a 1px image; if the API errors, no vision."""
        if self._vision is not None:
            return self._vision
        png1px = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
            "YGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
        )
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "Describe this image in one word."},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{png1px}"}},
        ]}]
        try:
            self.send(msgs, tools=None, max_tokens=8)
            self._vision = True
        except Exception as e:
            log.warning("vision probe failed (%s): text-only grounding", str(e)[:120])
            self._vision = False
        return self._vision

    # ------------------------------------------------------- content helpers

    @staticmethod
    def image_block(png_b64: str) -> dict:
        return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_b64}"}}


class FallbackChain:
    """Try providers in order on 429/5xx/network (§3 model routing)."""

    def __init__(self, providers: list[LLMProvider]):
        self.providers = providers

    def send(self, *args, **kwargs) -> LLMResponse:
        last = None
        for p in self.providers:
            try:
                return p.send(*args, **kwargs)
            except Exception as e:
                last = e
                log.warning("provider %s failed (%s); trying next",
                            type(p).__name__, str(e)[:100])
        raise last
