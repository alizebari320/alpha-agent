"""Recipe cache (§7) — learned deterministic macros.

When a task succeeds, the normalized request + successful action sequence is
stored in ~/.local/share/alpha/recipes/ as JSON. On a new request, a fuzzy
match runs FIRST; on match, the recipe is REPLAYED DIRECTLY with no LLM call,
verifying each step. "Search the web for X", "open Blender", "mute Spotify"
become instant and free after the first run.

CLI: `alpha recipes list|delete|export <id>|import <file>`
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import paths

log = logging.getLogger(__name__)

STOPWORDS = {"the", "a", "an", "please", "for", "to", "on", "in", "and", "with",
             "open", "search", "web", "go", "find", "new", "me", "my"}


def normalize_request(text: str) -> str:
    """Lowercase, strip punctuation/stopwords, collapse whitespace."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    words = [w for w in text.split() if w not in STOPWORDS]
    return " ".join(words) or text


@dataclass
class Recipe:
    id: str
    request: str            # normalized
    display_request: str    # original first seen
    actions: list[dict]     # [{tool, args...}] in order
    created: float = field(default_factory=time.time)
    runs: int = 0
    last_run: float = 0.0

    def path(self, root: Path | None = None) -> Path:
        base = root or paths.RECIPE_DIR
        return base / f"{self.id}.json"

    def save(self, root: Path | None = None) -> None:
        self.path(root).write_text(json.dumps(asdict(self), indent=1))

    @classmethod
    def load(cls, p: Path) -> Recipe:
        return cls(**json.loads(p.read_text()))


class RecipeStore:
    def __init__(self, root: Path | None = None):
        self.root = root or paths.RECIPE_DIR
        self.root.mkdir(parents=True, exist_ok=True)

    def all(self) -> list[Recipe]:
        out = []
        for p in sorted(self.root.glob("*.json")):
            try:
                out.append(Recipe.load(p))
            except Exception as e:
                log.warning("bad recipe %s: %s", p.name, e)
        return out

    def add(self, request: str, actions: list[dict]) -> Recipe:
        r = Recipe(id=uuid.uuid4().hex[:12], request=normalize_request(request),
                   display_request=request.strip(), actions=actions)
        r.save(self.root)
        log.info("recipe saved: %r (%d actions)", r.request, len(actions))
        return r

    def match(self, request: str, threshold: float = 0.72) -> Recipe | None:
        """Fuzzy match the normalized request against stored recipes."""
        nq = normalize_request(request)
        if not nq:
            return None
        best, best_ratio = None, 0.0
        for r in self.all():
            ratio = difflib.SequenceMatcher(None, nq, r.request).ratio()
            # also try containment: "open blender" matches "open blender now"
            if r.request in nq or nq in r.request:
                ratio = max(ratio, 0.9)
            if ratio > best_ratio:
                best, best_ratio = r, ratio
        if best and best_ratio >= threshold:
            log.info("recipe match: %r ~ %r (%.2f)", nq, best.request, best_ratio)
            return best
        return None

    def record_run(self, recipe: Recipe, ok: bool) -> None:
        recipe.runs += 1
        recipe.last_run = time.time()
        if ok:
            recipe.save(self.root)
        else:
            # failed replay: drop the recipe so the next run re-plans
            log.info("recipe %s failed replay; removing", recipe.id)
            self.delete(recipe.id)

    def delete(self, rid: str) -> bool:
        p = self.root / f"{rid}.json"
        if p.exists():
            p.unlink()
            return True
        return False

    def export(self, rid: str, dest: Path) -> Path | None:
        p = self.root / f"{rid}.json"
        if not p.exists():
            return None
        data = p.read_bytes()
        dest.write_bytes(data)
        return dest

    def import_from(self, src: Path) -> Recipe | None:
        try:
            r = Recipe.load(src)
        except Exception as e:
            log.error("recipe import failed: %s", e)
            return None
        r.id = uuid.uuid4().hex[:12]  # avoid id collisions
        r.save()
        return r


# ---------------------------------------------------------------------------
# Replay: execute a recipe's actions with verification, no LLM
# ---------------------------------------------------------------------------


async def replay_recipe(recipe: Recipe, agent_loop) -> tuple[bool, str]:
    """Run each stored action; return (ok, answer). Verification per §7:
    every step is audited/aborted like a normal action; a failure drops the
    recipe (the next identical request re-plans with the LLM)."""
    daemon = agent_loop.daemon
    guard = daemon.guard
    from ..safety.guard import AbortRequested

    try:
        for act in recipe.actions:
            guard.check_abort()
            from .providers.openai_compatible import ToolCall

            call = ToolCall(name=act.get("tool", ""), arguments=act.get("args", {}))
            result = await agent_loop._execute(call, await agent_loop._observe())
            if isinstance(result, str) and result.startswith("error"):
                return False, "That recipe stopped working."
        return True, recipe.display_request and f"Done: {recipe.display_request}."
    except AbortRequested:
        raise
    except Exception as e:
        log.warning("recipe replay error: %s", e)
        return False, "That recipe stopped working."
