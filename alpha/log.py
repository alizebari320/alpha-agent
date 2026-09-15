"""Logging with secret redaction.

Alpha must never leak API keys into logs, the journal, or crash reports.
Everything funnels through configure_logging(), which installs a filter that
redacts anything shaped like a secret before it is emitted.
"""

from __future__ import annotations

import logging
import re
import sys

# Key-shaped tokens: `sk-...`, `Bearer <token>`, key=value in URLs, etc.
_SECRET_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_\-]{8,}|"
    r"Bearer\s+[A-Za-z0-9_\-]{12,}|"
    r"api[_-]?key[=:]\s*[A-Za-z0-9_\-]+|"
    r"token[=:]\s*[A-Za-z0-9_\-]{12,})"
)
_REDACT = "<redacted>"


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _SECRET_RE.sub(_REDACT, str(record.getMessage()))
            record.args = ()
        except Exception:  # never let redaction crash logging
            pass
        return True


def configure_logging(verbose: bool = False) -> None:
    """Configure root logging to stderr (systemd captures this for the journal)."""
    root = logging.getLogger()
    root.handlers.clear()
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(RedactionFilter())
    root.addHandler(handler)
    root.setLevel(level)
    # Quiet noisy third-party loggers by default.
    for noisy in ("httpx", "httpcore", "keyring.backends", "onnxruntime"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def redact(text: str) -> str:
    """Redact secrets from an arbitrary string (for crash reports, TTY output)."""
    return _SECRET_RE.sub(_REDACT, text)