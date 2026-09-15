"""First-run model downloads (§11.6).

Whisper/piper/openwakeword weights are hundreds of MB. They download ONCE,
up front, with a progress bar — never silently mid-conversation. Everything
lands under ~/.local/share/alpha/models/.
"""

from __future__ import annotations

import logging
import sys
import urllib.request
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)


def _download(url: str, dest: Path, desc: str = "") -> None:
    """Download url -> dest with a tiny text progress bar. Atomic (tmp+rename)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 (https only, below)
        if not url.startswith("https://"):
            raise ValueError(f"refusing non-https URL: {url}")
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        chunk = 1 << 18
        with open(tmp, "wb") as f:
            while True:
                b = r.read(chunk)
                if not b:
                    break
                f.write(b)
                got += len(b)
                if total:
                    pct = got * 100 // total
                    bar = "#" * (pct // 5)
                    sys.stderr.write(f"\r  {desc or dest.name}: [{bar:<20}] {pct}% ({got//2**20}MB/{total//2**20}MB)")
                    sys.stderr.flush()
    sys.stderr.write("\n")
    tmp.replace(dest)
    log.info("downloaded %s (%s bytes)", url, got)


# ---------------------------------------------------------------------------
# Piper voices
# ---------------------------------------------------------------------------

_PIPER_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "{lang_family}/{lang_code}/{name}/{quality}/{lang_code}-{name}-{quality}{ext}?download=true"
)


def ensure_piper_voice(shortcode: str) -> tuple[Path, Path]:
    """Return (model.onnx, model.onnx.json) paths, downloading if needed.

    shortcode e.g. 'en_US-lessac-medium' or 'ar_JO-kareem-medium'.
    """
    parts = shortcode.split("-")
    if len(parts) != 3:
        raise ValueError(f"bad piper voice id: {shortcode!r} (want lang_CODE-name-quality)")
    lang_code, name, quality = parts
    lang_family = lang_code.split("_")[0]
    dest_dir = paths.MODEL_DIR / "piper" / shortcode
    base = f"{shortcode}"
    onnx = dest_dir / f"{base}.onnx"
    cfg = dest_dir / f"{base}.onnx.json"
    for ext, dest in ((".onnx", onnx), (".onnx.json", cfg)):
        url = _PIPER_URL.format(
            lang_family=lang_family, lang_code=lang_code, name=name,
            quality=quality, ext=ext,
        )
        _download(url, dest, desc=f"piper voice {shortcode}")
    return onnx, cfg


# ---------------------------------------------------------------------------
# Whisper (faster-whisper downloads via huggingface_hub with its own progress)
# ---------------------------------------------------------------------------


def pick_whisper_model(default: str = "auto") -> str:
    """§9: tiny.en default under 8 GB RAM, 'small' above. Auto-detect, user-
    overridable via stt.model."""
    if default != "auto":
        return default
    try:
        total_kb = int(
            next(l for l in open("/proc/meminfo") if l.startswith("MemTotal")).split()[1]
        )
        total_gb = total_kb / (2**20)
        return "small" if total_gb >= 8 else "tiny.en"
    except Exception:
        return "tiny.en"


def ensure_whisper(model: str):
    """Instantiate (downloading if needed) and return a WhisperModel."""
    from faster_whisper import WhisperModel

    log.info("loading whisper model %r (downloads on first run)", model)
    return WhisperModel(model, device="cpu", compute_type="int8")


# ---------------------------------------------------------------------------
# openWakeWord pretrained models
# ---------------------------------------------------------------------------


def ensure_openwakeword(wanted_models: list[str] | None = None) -> None:
    """Download openwakeword embed features + the requested wake word ONNXs."""
    import openwakeword.utils  # noqa: F401
    from openwakeword.utils import download_models

    download_models(model_names=wanted_models or [])
