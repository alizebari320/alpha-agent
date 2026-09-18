"""Discover existing LLM credentials on the machine and store them in keyring.

At install time we import whatever the user already has configured in
`opencode` so Alpha works with ZERO manual key entry. opencode's config lives in
a couple of places:

* ``~/.config/opencode/opencode.json`` — provider definitions. Each provider in
  the top-level ``provider`` object may carry ``options.baseURL`` and either a
  literal ``apiKey`` or an ``{env:VAR}`` indirection.
* ``~/.local/share/opencode/auth.json`` — the auth store (``{"provider": {"type": "api", "key": "..."}}``).
  This is usually the freshest source of truth for the key.

The discovered API key is written to the system keyring (libsecret via the
``keyring`` package) and NEVER to disk as plaintext, so nothing secret ends up
in git or a world-readable file.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

KEYRING_SERVICE = "alpha-agent"

OPENCODE_CONFIG = Path("~/.config/opencode/opencode.json").expanduser()
OPENCODE_AUTH = Path("~/.local/share/opencode/auth.json").expanduser()

# Providers we prefer when picking Alpha's default runtime, in order.
PREFERRED_PROVIDER_KEYS = ("agentrouter-openai", "agentrouter", "openrouter")
PREFERRED_MODELS = ("glm-5.3", "claude-sonnet-4-5", "claude-sonnet-5", "apmix/claude-opus-4-8-free", "apmix/llama-3.3-70b-free", "apmix/deepseek-r1-free")


@dataclass
class Provider:
    """A discovered LLM provider plus its secret, ready to be imported."""

    id: str
    base_url: str
    api_key: str
    model: str
    user_agent: str = "opencode/1.18.30"
    npm: str = "@ai-sdk/openai-compatible"


@dataclass
class ImportResult:
    providers: list[Provider] = field(default_factory=list)
    chosen: Provider | None = None
    warnings: list[str] = field(default_factory=list)


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("could not read %s: %s", path, e)
        return {}


def _resolve_key(key_or_ref: str | None, auth_store: dict, env: dict[str, str]) -> str | None:
    """Resolve an apiKey that may be a literal or an ``{env:VAR}`` reference."""
    if not key_or_ref:
        return None
    if key_or_ref.startswith("{env:") and key_or_ref.endswith("}"):
        var = key_or_ref[5:-1]
        val = env.get(var)
        if val:
            return val
        log.warning("env var %s referenced by opencode but not set", var)
        return None
    return key_or_ref


def _provider_models(entry: dict, auth_store: dict, env: dict[str, str]) -> list[str]:
    return sorted((entry.get("models") or {}).keys())


def discover_opencode(
    config_path: Path = OPENCODE_CONFIG, auth_path: Path = OPENCODE_AUTH
) -> ImportResult:
    """Read opencode's config + auth store and return importable providers."""
    result = ImportResult()
    config = _load_json(config_path)
    auth_store = _load_json(auth_path)
    env = dict(os.environ)

    providers_raw = config.get("provider", {}) or {}
    # The astute reader may notice opencode stores providers under various keys;
    # normalize to a dict of provider-id -> definition.
    if not isinstance(providers_raw, dict):
        # Newer opencode may nest under "provider" differently; attempt list form.
        providers_raw = {}

    for pid, entry in providers_raw.items():
        if not isinstance(entry, dict):
            continue
        options = entry.get("options", {}) or {}
        base_url = options.get("baseURL") or options.get("base_url") or ""
        if not base_url:
            continue

        key: str | None = options.get("apiKey") or options.get("api_key")
        key = _resolve_key(key, auth_store, env)
        # opencode uses the literal apiKey in provider options at runtime; the
        # auth store (~/.local/share/opencode/auth.json) is often STALE for
        # providers that carry an explicit key. Fall back to the auth store
        # only when options.apiKey is absent.
        if not key:
            auth_entry = auth_store.get(pid) or {}
            key = auth_entry.get("key") if isinstance(auth_entry, dict) else None

        models = _provider_models(entry, auth_store, env)
        model = models[0] if models else ""
        # Prefer the default model set at top level, else a preferred one.
        top_model = config.get("model", "")
        if top_model and top_model.split("/", 1)[0] == pid:
            model = top_model.split("/", 1)[1]
        elif any(m in PREFERRED_MODELS for m in models):
            model = next(m for m in PREFERRED_MODELS if m in models)

        npm = entry.get("npm", "@ai-sdk/openai-compatible")
        result.providers.append(
            Provider(id=pid, base_url=base_url.rstrip("/"), api_key=key or "", model=model, npm=npm)
        )

    # Choose a default: preferred provider id first, then one with a key + model.
    for pid in PREFERRED_PROVIDER_KEYS:
        for p in result.providers:
            if p.id == pid and p.api_key:
                result.chosen = p
                break
        if result.chosen:
            break
    if not result.chosen:
        for p in result.providers:
            if p.api_key:
                result.chosen = p
                break
    return result


# --------------------------------------------------------------------------
# Keyring
# --------------------------------------------------------------------------


def _get_keyring():
    try:
        import keyring

        return keyring
    except ImportError as e:  # pragma: no cover - defensive
        raise RuntimeError("python `keyring` package is not installed") from e


def store_key(provider_id: str, api_key: str) -> None:
    _get_keyring().set_password(KEYRING_SERVICE, provider_id, api_key)


def get_key(provider_id: str) -> str | None:
    try:
        return _get_keyring().get_password(KEYRING_SERVICE, provider_id)
    except Exception as e:
        log.warning("keyring read failed for %r: %s", provider_id, e)
        return None


def delete_key(provider_id: str) -> None:
    try:
        _get_keyring().delete_password(KEYRING_SERVICE, provider_id)
    except Exception as e:
        log.warning("keyring delete failed for %r: %s", provider_id, e)


def keyring_available() -> tuple[bool, str]:
    """Return (usable, backend_name). Used by doctor and init."""
    try:
        kr = _get_keyring()
        backend = kr.get_keyring().name
        # Probe write/read on a throwaway entry to ensure a real backend exists.
        try:
            kr.set_password(KEYRING_SERVICE, "__probe__", "x")
            kr.get_password(KEYRING_SERVICE, "__probe__")
            kr.delete_password(KEYRING_SERVICE, "__probe__")
        except Exception:
            return False, backend
        return True, backend
    except Exception as e:
        return False, str(e)


# --------------------------------------------------------------------------
# Import into Alpha's config (appends a [llm] block for the chosen provider)
# --------------------------------------------------------------------------


def build_llm_config(provider: Provider) -> dict:
    """Return the [llm] TOML fragment to write into Alpha's config."""
    return {
        "planner": {
            "provider": provider.id,
            "model": provider.model,
            "base_url": provider.base_url,
            "user_agent": provider.user_agent,
            "api_key_keyring_user": provider.id,
        }
    }


def import_provider_into_config(provider: Provider, config_path: Path) -> None:
    """Store the key in keyring and write an [llm] block into config.toml."""
    store_key(provider.id, provider.api_key)
    # Reuse config.py defaults for everything else; we only pin the planner role.
    try:
        from .config import load_config, parse_config

        # Load existing config if present, else start from defaults.
        if config_path.exists():
            try:
                cfg = load_config(config_path)
            except Exception as e:
                log.warning("existing config unreadable (%s), starting fresh", e)
                cfg = parse_config({})
        else:
            from .config import Config

            cfg = Config()
    except ImportError:  # pragma: no cover
        cfg = None  # type: ignore[assignment]

    # Write a minimal but complete config if one doesn't exist yet.
    if cfg is not None:
        cfg.llm.planner.provider = provider.id
        cfg.llm.planner.model = provider.model
        cfg.llm.planner.base_url = provider.base_url
        cfg.llm.planner.user_agent = provider.user_agent
        cfg.llm.planner.api_key_keyring_user = provider.id
        # A provider with the same model works fine as grounder/summarizer too.
        for role in (cfg.llm.grounder, cfg.llm.summarizer):
            role.provider = provider.id
            role.model = provider.model
            role.base_url = provider.base_url
            role.user_agent = provider.user_agent
            role.api_key_keyring_user = provider.id

    write_config(cfg, config_path)


def write_config(cfg, config_path: Path) -> None:
    """Serialize a Config object to TOML (small, hand-rolled serializer)."""
    from .config import (
        LLMRole,
    )

    def role(r: LLMRole) -> dict:
        return {
            "provider": r.provider,
            "model": r.model,
            "base_url": r.base_url,
            "user_agent": r.user_agent,
            "api_key_keyring_user": r.api_key_keyring_user,
        }

    doc: dict = {}
    a = cfg.assistant
    doc["assistant"] = {
        "name": a.name,
        "language": a.language,
        "wake_word": {
            "mode": a.wake_word.mode,
            "phrase": a.wake_word.phrase,
            "model": a.wake_word.model,
            "threshold": a.wake_word.threshold,
            "refractory_s": a.wake_word.refractory_s,
        },
    }
    doc["stt"] = {
        "model": cfg.stt.model,
        "language": cfg.stt.language,
        "compute_type": cfg.stt.compute_type,
    }
    doc["tts"] = {
        "voice": cfg.tts.voice,
        "arabic_voice": cfg.tts.arabic_voice,
        "volume": cfg.tts.volume,
    }
    doc["llm"] = {
        "planner": role(cfg.llm.planner),
        "grounder": role(cfg.llm.grounder),
        "summarizer": role(cfg.llm.summarizer),
        "fallbacks": [role(fb) for fb in cfg.llm.fallbacks],
        "max_steps": cfg.llm.max_steps,
        "daily_spend_cap_usd": cfg.llm.daily_spend_cap_usd,
        "per_request_step_cap": cfg.llm.per_request_step_cap,
        "request_timeout_s": cfg.llm.request_timeout_s,
    }
    doc["vision"] = {
        "enabled": cfg.vision.enabled,
        "max_width": cfg.vision.max_width,
        "history_images": cfg.vision.history_images,
    }
    doc["safety"] = {
        "abort_hotkey": cfg.safety.abort_hotkey,
        "confirm_destructive": cfg.safety.confirm_destructive,
        "password_field_lock": cfg.safety.password_field_lock,
        "denylist": cfg.safety.denylist,
        "bash_allowlist": cfg.safety.bash_allowlist,
    }
    doc["resources"] = {
        "idle_unload_s": cfg.resources.idle_unload_s,
        "memory_max_mb": cfg.resources.memory_max_mb,
    }

    out = _toml_dump(doc)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        config_path.parent.chmod(0o700)
    except OSError:
        pass
    tmp = config_path.with_suffix(".toml.tmp")
    tmp.write_text(out)
    tmp.replace(config_path)


def _toml_dump(doc: dict) -> str:
    """Serialize to TOML using tomli_w."""
    import tomli_w

    return tomli_w.dumps(doc)
