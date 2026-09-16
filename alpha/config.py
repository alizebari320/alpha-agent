"""Configuration loading, validation and defaults.

The user-facing file is ~/.config/alpha/config.toml (see config.toml.example).
Secrets are NEVER stored here: the API key lives in the system keyring
(see alpha.credentials).
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when the config file is missing, malformed or invalid."""


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


VALID_WAKE_MODES = {"pretrained", "kws"}
VALID_VISION_MODES = {"auto", "on", "off"}


@dataclass
class WakeWordConfig:
    # Tier 1: shipped pretrained openWakeWord models (wake.BUNDLED_WAKEWORDS).
    # Tier 3: "kws" keyword spotting (whisper-tiny fuzzy match) — works with
    # ANY name immediately. This is the DEFAULT because it makes the
    # configured wake phrase work out of the box with zero training.
    mode: str = "kws"  # pretrained | kws
    phrase: str = "hey alpha"
    # Only used when mode="pretrained". Must be one of wake.BUNDLED_WAKEWORDS
    # unless a custom trained model exists in ~/.local/share/alpha/wakewords/.
    model: str = "hey_jarvis"
    threshold: float = 0.6
    refractory_s: float = 2.0


@dataclass
class AssistantConfig:
    name: str = "alpha"
    language: str = "en"  # UI language for the HUD
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)


@dataclass
class STTConfig:
    model: str = "auto"  # auto | tiny | base | small | medium
    language: str = "auto"  # "auto" lets whisper detect (Arabic + English)
    compute_type: str = "int8"


@dataclass
class TTSConfig:
    voice: str = "en_US-lessac-medium"  # default English piper voice
    arabic_voice: str = "ar_JO-kareem-medium"  # used when speaking Arabic
    volume: float = 1.0


@dataclass
class LLMRole:
    """One LLM endpoint for one role (planner / grounder / summarizer)."""

    provider: str = "agentrouter"
    model: str = "glm-5.3"
    base_url: str = "https://agentrouter.org/v1"
    # Some gateways (e.g. AgentRouter) reject clients whose User-Agent is not
    # on their allowlist. Discovered at import time; see alpha.credentials.
    user_agent: str = "opencode/1.18.30"
    api_key_keyring_user: str = "agentrouter"


@dataclass
class LLMConfig:
    planner: LLMRole = field(default_factory=LLMRole)
    grounder: LLMRole = field(default_factory=LLMRole)
    summarizer: LLMRole = field(default_factory=LLMRole)
    # Fallback chain: tried in order when the primary returns 429/5xx/network.
    fallbacks: list[LLMRole] = field(default_factory=list)
    max_steps: int = 25
    daily_spend_cap_usd: float = 5.0
    per_request_step_cap: int = 25
    request_timeout_s: float = 120.0


@dataclass
class VisionConfig:
    enabled: str = "auto"  # auto | on | off
    max_width: int = 1280
    history_images: int = 3  # screenshots kept in message history


@dataclass
class SafetyConfig:
    abort_hotkey: str = "ctrl+alt+q"
    confirm_destructive: bool = True
    password_field_lock: bool = True
    # App names / window classes where Alpha refuses to act at all.
    denylist: list[str] = field(default_factory=list)
    # Commands runnable without spoken confirmation (allowlist).
    bash_allowlist: list[str] = field(default_factory=lambda: [
        "firefox", "google-chrome", "chromium", "nautilus", "gnome-control-center",
        "blender", "libreoffice", "gimp", "inkscape", "code", "xdg-open",
    ])


@dataclass
class ResourceConfig:
    idle_unload_s: int = 120
    memory_max_mb: int = 2048  # systemd MemoryMax safety net (measured warm
                               # footprint is ~910 MiB: whisper-small + piper + GTK HUD)


@dataclass
class Config:
    assistant: AssistantConfig = field(default_factory=AssistantConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    resources: ResourceConfig = field(default_factory=ResourceConfig)


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------


def _get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Nested dict get; returns default if any level is missing/not a dict."""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _parse_llm_role(data: dict[str, Any], default: LLMRole) -> LLMRole:
    return LLMRole(
        provider=data.get("provider", default.provider),
        model=data.get("model", default.model),
        base_url=data.get("base_url", default.base_url),
        user_agent=data.get("user_agent", default.user_agent),
        api_key_keyring_user=data.get("api_key_keyring_user", default.api_key_keyring_user),
    )


VALID_VISION_MODES = {"auto", "on", "off"}


def parse_config(data: dict[str, Any]) -> Config:
    """Build a validated Config from a parsed-TOML dict. Raises ConfigError."""
    cfg = Config()

    a = _get(data, "assistant", default={}) or {}
    cfg.assistant.name = str(a.get("name", cfg.assistant.name)).strip().lower() or "alpha"
    cfg.assistant.language = str(a.get("language", cfg.assistant.language))
    w = a.get("wake_word", {}) or {}
    cfg.assistant.wake_word = WakeWordConfig(
        mode=str(w.get("mode", "kws")),
        phrase=str(w.get("phrase", f"hey {cfg.assistant.name}")),
        model=str(w.get("model", "hey_jarvis")),
        threshold=float(w.get("threshold", 0.6)),
        refractory_s=float(w.get("refractory_s", 2.0)),
    )
    if cfg.assistant.wake_word.mode not in VALID_WAKE_MODES:
        raise ConfigError(f"wake_word.mode must be one of {sorted(VALID_WAKE_MODES)}")
    if cfg.assistant.wake_word.mode == "pretrained":
        from .wake import BUNDLED_WAKEWORDS

        if cfg.assistant.wake_word.model not in BUNDLED_WAKEWORDS:
            model_path = paths.WAKEWORD_DIR / f"{cfg.assistant.wake_word.model}.onnx"
            if not model_path.exists():
                raise ConfigError(
                    f"unknown pretrained wake word model '{cfg.assistant.wake_word.model}'; "
                    f"bundled: {list(BUNDLED_WAKEWORDS)}, or train one with "
                    f"scripts/train-wakeword.py, or switch to mode 'kws'"
                )

    s = _get(data, "stt", default={}) or {}
    cfg.stt = STTConfig(
        model=str(s.get("model", "auto")),
        language=str(s.get("language", "auto")),
        compute_type=str(s.get("compute_type", "int8")),
    )
    if cfg.stt.model not in {"auto", "tiny", "base", "small", "medium"}:
        raise ConfigError(f"stt.model '{cfg.stt.model}' invalid")

    t = _get(data, "tts", default={}) or {}
    cfg.tts = TTSConfig(
        voice=str(t.get("voice", cfg.tts.voice)),
        arabic_voice=str(t.get("arabic_voice", cfg.tts.arabic_voice)),
        volume=float(t.get("volume", 1.0)),
    )

    llm = _get(data, "llm", default={}) or {}
    cfg.llm.planner = _parse_llm_role(_get(llm, "planner", default={}) or {}, cfg.llm.planner)
    cfg.llm.grounder = _parse_llm_role(_get(llm, "grounder", default={}) or {}, cfg.llm.grounder)
    cfg.llm.summarizer = _parse_llm_role(_get(llm, "summarizer", default={}) or {}, cfg.llm.summarizer)
    cfg.llm.fallbacks = [
        _parse_llm_role(fb, LLMRole()) for fb in (llm.get("fallbacks") or []) if isinstance(fb, dict)
    ]
    cfg.llm.max_steps = int(llm.get("max_steps", cfg.llm.max_steps))
    cfg.llm.daily_spend_cap_usd = float(llm.get("daily_spend_cap_usd", cfg.llm.daily_spend_cap_usd))
    cfg.llm.per_request_step_cap = int(llm.get("per_request_step_cap", cfg.llm.per_request_step_cap))
    cfg.llm.request_timeout_s = float(llm.get("request_timeout_s", cfg.llm.request_timeout_s))
    if cfg.llm.max_steps < 1 or cfg.llm.max_steps > 100:
        raise ConfigError("llm.max_steps must be between 1 and 100")
    if cfg.llm.daily_spend_cap_usd <= 0:
        raise ConfigError("llm.daily_spend_cap_usd must be positive")

    v = _get(data, "vision", default={}) or {}
    cfg.vision = VisionConfig(
        enabled=str(v.get("enabled", "auto")),
        max_width=int(v.get("max_width", 1280)),
        history_images=int(v.get("history_images", 3)),
    )
    if cfg.vision.enabled not in VALID_VISION_MODES:
        raise ConfigError(f"vision.enabled must be one of {sorted(VALID_VISION_MODES)}")
    if not 320 <= cfg.vision.max_width <= 3840:
        raise ConfigError("vision.max_width must be between 320 and 3840")

    sf = _get(data, "safety", default={}) or {}
    cfg.safety = SafetyConfig(
        abort_hotkey=str(sf.get("abort_hotkey", cfg.safety.abort_hotkey)),
        confirm_destructive=bool(sf.get("confirm_destructive", True)),
        password_field_lock=bool(sf.get("password_field_lock", True)),
        denylist=[str(x) for x in (sf.get("denylist") or [])],
        bash_allowlist=[str(x) for x in (sf.get("bash_allowlist") or cfg.safety.bash_allowlist)],
    )

    r = _get(data, "resources", default={}) or {}
    cfg.resources = ResourceConfig(
        idle_unload_s=int(r.get("idle_unload_s", 120)),
        memory_max_mb=int(r.get("memory_max_mb", 2048)),
    )

    return cfg


# --------------------------------------------------------------------------
# Load / save
# --------------------------------------------------------------------------


def load_config(path: Path | None = None) -> Config:
    path = path or paths.CONFIG_FILE
    if not path.exists():
        raise ConfigError(
            f"no config at {path}; run `alpha init` (or copy config.toml.example)"
        )
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e
    return parse_config(data)
