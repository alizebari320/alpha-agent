"""Blender API-first path (§7 special case).

NEVER click inside Blender's UI. For Blender requests we generate a bpy
script, run it with `blender -b --python <script>` (headless) or against the
user's running instance via a small add-on later, and render a preview the
agent can feed back for self-correction.

This is the template for all API-first apps: if a target app has a scripting
API (Blender bpy, LibreOffice UNO, GIMP script-fu, browsers via CDP), prefer
it over GUI clicking. Documented in docs/architecture.md.

Ships one working example: "make me a low-poly tree".
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

BLENDER_INTENT_RE = re.compile(r"\bblender\b", re.IGNORECASE)

# Template library: request keywords -> bpy generator. Extend carefully; these
# are deterministic scripts, safe to run headless.
_TEMPLATES = {
    "low-poly tree": {
        "match": re.compile(r"low[ -]?poly\s+tree|tree", re.IGNORECASE),
        "file": "lowpoly_tree.py",
    },
}

_HERE = Path(__file__).parent


def is_blender_request(request: str) -> bool:
    return bool(BLENDER_INTENT_RE.search(request)) and shutil.which("blender") is not None


def _pick_template(request: str):
    for key, t in _TEMPLATES.items():
        if t["match"].search(request):
            return _HERE / "blender_templates" / t["file"]
    return None


def run_blender_script(request: str) -> tuple[bool, str, Path | None]:
    """Generate + run a bpy script for the request. Returns (ok, answer, preview)."""
    tpl = _pick_template(request)
    if tpl is None or not tpl.exists():
        return False, ("I don't have a Blender script for that yet — "
                       "add one to alpha/brain/blender_templates/."), None

    with tempfile.TemporaryDirectory(prefix="alpha_blender_") as td:
        out_png = Path(td) / "preview.png"
        script = Path(td) / "script.py"
        # inject the render path into the template
        code = tpl.read_text().replace("__ALPHA_PREVIEW__", str(out_png))
        script.write_text(code)
        log.info("running blender headless: %s", script.name)
        try:
            r = subprocess.run(
                ["blender", "-b", "--python", str(script)],
                capture_output=True, text=True, timeout=120,
            )
        except FileNotFoundError:
            return False, "Blender isn't installed.", None
        if r.returncode != 0 or not out_png.exists():
            log.warning("blender failed: %s", (r.stderr or "")[-400:])
            return False, "Blender reported an error running that script.", None
        # keep the preview where the agent can see it
        keep = Path(tempfile.gettempdir()) / f"alpha_blender_preview_{int(time.time())}.png"
        keep.write_bytes(out_png.read_bytes())
        return True, "Done — I built it in Blender and rendered a preview.", keep
