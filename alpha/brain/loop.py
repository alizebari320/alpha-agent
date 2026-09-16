"""Planner/executor agent loop (§7).

Two tiers: a PLANNER call turns the request + screen state into a short step
list; an EXECUTOR performs one action at a time, re-planning only on failure.
Max 25 steps/request. Every action is audit-logged BEFORE execution and the
abort flag is checked between every tool call (§10).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass

from .. import paths
from ..safety.guard import AbortRequested
from .prompts import system_prompt
from .providers.openai_compatible import FallbackChain, OpenAICompatibleProvider
from .tools import tool_schemas

log = logging.getLogger(__name__)

MAX_STEPS = 25
MAX_ACTIONS_PER_STEP = 6
NO_CHANGE_LIMIT = 3
# GUI apps never exit on their own: `bash firefox` would block the tool until
# its timeout and then look like a failure. These are launched detached.
GUI_LAUNCHERS = {
    "firefox", "firefox-esr", "google-chrome", "google-chrome-stable", "chromium",
    "chromium-browser", "nautilus", "gnome-control-center", "gnome-text-editor",
    "blender", "libreoffice", "soffice", "gimp", "inkscape", "code", "xdg-open",
    "gnome-calculator", "evince", "eog", "totem", "gnome-system-monitor",
}


def _run_bash(command: str, timeout: float = 30.0) -> str:
    """Run a shell command for the agent.

    GUI launchers are started detached and reported immediately — otherwise a
    perfectly good `firefox` launch is misreported as a 30s timeout failure.
    """
    base = os.path.basename(command.strip().split()[0]) if command.strip() else ""
    is_gui = base in GUI_LAUNCHERS
    if is_gui:
        try:
            p = subprocess.Popen(  # noqa: S602 - the command IS the tool's input
                command, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
            time.sleep(1.5)  # let it map a window before the next observation
            alive = p.poll() is None
            return (f"launched in background (pid={p.pid}, running={alive})"
                    if alive else f"process exited immediately (exit={p.returncode})")
        except Exception as e:
            return f"failed to launch: {e}"
    try:
        # shell=True is the point: the planner emits a shell command, and the
        # guard (allowlist + destructive regex) has already vetted it.
        r = subprocess.run(command, shell=True, capture_output=True, text=True,  # noqa: S602
                           timeout=timeout)
        return f"exit={r.returncode} " + (r.stdout or r.stderr or "")[:300]
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout:.0f}s (command may still be running)"
    except Exception as e:
        return f"failed: {e}"


@dataclass
class Observation:
    png_b64: str | None = None
    shot_size: tuple[int, int] | None = None
    element_table: str = ""
    label_map: dict | None = None
    window: str = ""


class CostGuard:
    """Daily spend cap + per-request step cap (§3). Logs tokens/cost per request."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._today: dict[str, float] = {}
        self.path = paths.SPEND_LOG

    def record(self, model: str, usage: dict, est_cost: float) -> None:
        entry = {"ts": time.time(), "model": model,
                 "prompt_tokens": usage.get("prompt_tokens", 0),
                 "completion_tokens": usage.get("completion_tokens", 0),
                 "est_cost_usd": round(est_cost, 6)}
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass
        day = time.strftime("%Y-%m-%d")
        self._today.setdefault(day, 0.0)
        self._today[day] += est_cost

    def spent_today(self) -> float:
        day = time.strftime("%Y-%m-%d")
        if day not in self._today:
            # reload from disk (daemon restarts)
            total = 0.0
            try:
                with open(self.path) as f:
                    for line in f:
                        try:
                            e = json.loads(line)
                            if time.strftime("%Y-%m-%d", time.localtime(e["ts"])) == day:
                                total += e.get("est_cost_usd", 0)
                        except Exception:
                            continue
                self._today[day] = total
            except FileNotFoundError:
                self._today[day] = 0.0
        return self._today[day]

    def over_limit(self) -> bool:
        return self.spent_today() >= self.cfg.llm.daily_spend_cap_usd


class AgentLoop:
    def __init__(self, daemon):
        self.daemon = daemon
        cfg = daemon.cfg
        self.cfg = cfg
        # providers: planner primary + fallback chain (§3)
        primary = OpenAICompatibleProvider(cfg.llm.planner)
        fallbacks = [OpenAICompatibleProvider(fb) for fb in cfg.llm.fallbacks]
        self.planner = FallbackChain([primary, *fallbacks])
        self.executor = self.planner
        self.tools = tool_schemas()
        self.cost = CostGuard(cfg)
        self.vision_enabled: bool | None = None  # probed lazily

    # ------------------------------------------------------------ helpers

    def _provider_vision(self) -> bool:
        if self.vision_enabled is None:
            mode = self.cfg.vision.enabled
            if mode == "on":
                self.vision_enabled = True
            elif mode == "off":
                self.vision_enabled = False
            else:  # auto: runtime probe (§3 VISION CHECK)
                self.vision_enabled = self.planner.providers[0].supports_vision()
                log.info("vision auto-probe: %s", self.vision_enabled)
        return self.vision_enabled

    async def _observe(self) -> Observation:
        obs = Observation()
        vision = self._provider_vision()
        if vision:
            shot = await self.daemon.vision.screenshot()
            if shot:
                obs.png_b64 = shot.png_b64
                obs.shot_size = (shot.width, shot.height)
        els = await self.daemon.vision.atspi()
        if els:
            from ..vision import som_label, som_table_text
            mon = self.daemon.vision._active_monitor()
            shot_size = obs.shot_size or (mon["w"], mon["h"])
            labels, label_map = som_label(els, shot_size[0], shot_size[1], mon)
            obs.element_table = som_table_text(labels)
            obs.label_map = label_map
            obs.window = f"{els[0].role}" if els else ""
        return obs

    @staticmethod
    def _shot_hash(png_b64: str | None) -> str:
        return hashlib.sha256(png_b64.encode()).hexdigest()[:16] if png_b64 else ""

    # ------------------------------------------------------------ dispatch

    async def _execute(self, call, obs: Observation) -> str:
        """Run one tool call. Returns a human-readable result for the model."""
        daemon = self.daemon
        guard = daemon.guard
        name = call.name
        args = call.arguments or {}
        mon = daemon.vision._active_monitor()
        input_backend = None
        result = "ok"

        # audit BEFORE executing (spec hard constraint)
        guard.audit_action(name, args)

        if name == "finish":
            return "FINISH"

        if name in ("click", "double_click", "right_click", "move", "drag"):
            input_backend = daemon._get_input()
            sw, sh = obs.shot_size or (mon["w"], mon["h"])
            from ..vision import model_to_screen
            x, y = model_to_screen(float(args.get("x", 0)), float(args.get("y", 0)),
                                   sw, sh, mon)
            if name == "click":
                input_backend.click(x, y, args.get("button", "left"))
            elif name == "double_click":
                input_backend.double_click(x, y)
            elif name == "right_click":
                input_backend.right_click(x, y)
            elif name == "move":
                input_backend.move_abs(x, y)
            else:
                x1, y1 = model_to_screen(float(args.get("x1", 0)), float(args.get("y1", 0)), sw, sh, mon)
                x2, y2 = model_to_screen(float(args.get("x2", 0)), float(args.get("y2", 0)), sw, sh, mon)
                input_backend.drag(x1, y1, x2, y2)
        elif name == "click_element":
            label = str(args.get("label", ""))
            if not obs.label_map or label not in obs.label_map:
                return f"error: unknown label {label!r} (not in element table)"
            pos = obs.label_map[label]
            daemon._get_input().click(pos["screen_x"], pos["screen_y"])
        elif name == "type_text":
            daemon._get_input().type_text(str(args.get("text", "")))
        elif name == "key":
            daemon._get_input().key(str(args.get("combo", "")))
        elif name == "scroll":
            daemon._get_input().scroll(int(args.get("dx", 0) or 0), int(args.get("dy", 0) or 0))
        elif name == "bash":
            command = str(args.get("command", ""))
            allowed, _reason = guard.check_action("bash", {"command": command})
            if not allowed:
                ok = await self._confirm_spoken(f"Should I run the command {command}?")
                if not ok:
                    return "user declined; do not retry this command"
            result = await asyncio.to_thread(_run_bash, command)
        elif name == "wait":
            # NB: `asyncio` is imported at module level. A local `import asyncio`
            # here would make the name function-local for the WHOLE of _execute,
            # so the `bash` branch above (asyncio.to_thread) raised
            # UnboundLocalError — every bash call failed. Do not re-add it; the
            # regression is covered by tests/test_agent_loop.py::test_bash_tool_runs.
            await asyncio.sleep(min(10.0, float(args.get("ms", 200)) / 1000.0))
        else:
            return f"error: unknown tool {name}"
        return result

    async def _confirm_spoken(self, question: str) -> bool:
        """Spoken confirmation for gated actions (§10). Waits for yes/no."""
        daemon = self.daemon
        try:
            await daemon.speak(question)
            daemon.mic.clear()
            pcm = await daemon.record_request()
            answer = await daemon.transcribe(pcm)
            ans = (answer or "").strip().lower()
            ok = any(w in ans for w in ("yes", "yeah", "sure", "ok", "نعم", "اي", "أي", "بلى"))
            await daemon.speak("Okay, running it." if ok else "Okay, I won't.")
            return ok
        except Exception as e:
            log.warning("confirmation flow failed: %s", e)
            return False

    # ------------------------------------------------------------ main

    async def run(self, request: str) -> str:
        """Execute one spoken request; returns the spoken answer."""
        daemon = self.daemon
        guard = daemon.guard

        # password lock (§10): refuse to act while a password field is focused
        if await daemon.vision.check_password_lock():
            return "A password field is focused, so I won't touch the screen."

        if self.cost.over_limit():
            return ("I've hit my daily spending cap. "
                    "Raise it in the config or wait until tomorrow.")

        # RECIPE CACHE (§7): fuzzy-match BEFORE any LLM call; on hit, replay
        # the stored action sequence directly — instant and free.
        from .recipes import RecipeStore, replay_recipe

        store = RecipeStore(root=getattr(self, "recipe_root", None))
        recipe = store.match(request)
        if recipe:
            daemon._hud_state("replaying recipe…")
            ok, answer = await replay_recipe(recipe, self)
            store.record_run(recipe, ok)
            if ok:
                return answer
            log.info("recipe replay failed; falling through to the full loop")

        # BLENDER API-FIRST PATH (§7): never click inside Blender's UI —
        # generate a bpy script and run it headless instead.
        from .blender import is_blender_request, run_blender_script

        if is_blender_request(request):
            _ok, answer, _preview = run_blender_script(request)
            return answer

        vision = self._provider_vision()
        mon = daemon.vision._active_monitor()
        obs = await self._observe()
        sysp = system_prompt(daemon.cfg.assistant.name,
                             daemon.screen_size or (mon["w"], mon["h"]),
                             obs.shot_size, mon, "wayland", vision,
                             screen_share_ok=bool(obs.png_b64))

        # ---------------- PLANNER ----------------
        plan_messages = [
            {"role": "system", "content": sysp},
            {"role": "user", "content": f"Request: {request}\n\n"
             + (f"Element table:\n{obs.element_table}\n\n" if obs.element_table else "")
             + "Produce a short numbered plan (max 6 steps) of concrete actions. "
               "Reply with ONLY the numbered list, no commentary."},
        ]
        # Off the event loop: `send()` is a synchronous httpx call that can take
        # a minute (free-tier latency, 120 s timeout). Running it inline froze the
        # daemon's event loop, so the control socket could not answer — and the
        # control socket is how Ctrl+Alt+Q / `alpha abort` arrives. The abort
        # path must never be dead while Alpha is acting (§10).
        plan_resp = await asyncio.to_thread(
            self.planner.send, plan_messages, tools=None, max_tokens=512)
        self.cost.record(self.cfg.llm.planner.model, plan_resp.usage, 0.0)
        plan_text = plan_resp.text.strip()
        log.info("plan: %s", plan_text[:300])
        daemon._hud_state(plan_text[:80])

        # ---------------- EXECUTOR ----------------
        history: list[dict] = []
        actions_done = 0
        no_change = 0
        last_hash = self._shot_hash(obs.png_b64)
        answer = None
        self._recipe_actions = []  # executed actions -> recipe cache (§7)
        self._recipe_request = request

        for _step_idx in range(MAX_STEPS):
            guard.check_abort()  # AbortRequested propagates to daemon
            if self.cost.over_limit():
                answer = "I've hit my daily spending cap mid-task."
                break

            obs = await self._observe()
            cur_hash = self._shot_hash(obs.png_b64)
            if obs.png_b64:
                # visual verification only possible with screenshots
                if cur_hash and cur_hash != last_hash:
                    no_change = 0
                elif actions_done > 0:
                    no_change += 1
                last_hash = cur_hash
            else:
                no_change = 0

            user_content = [{"type": "text", "text":
                f"Request: {request}\nPlan:\n{plan_text}\n"
                f"Element table:\n{obs.element_table or '(empty)'}"}]
            if obs.png_b64:
                user_content.append(
                    OpenAICompatibleProvider.image_block(obs.png_b64))
            history.append({"role": "user", "content": user_content})

            resp = await asyncio.to_thread(
                self.executor.send,
                [{"role": "system", "content": sysp}, *history[-10:]],
                tools=self.tools, max_tokens=1024)
            self.cost.record(self.cfg.llm.planner.model, resp.usage, 0.0)

            if not resp.tool_calls:
                # plain text answer without finish(): use it
                answer = resp.text.strip() or "Done."
                history.append({"role": "assistant", "content": answer})
                break

            # execute the tool calls from this response
            for call in resp.tool_calls:
                guard.check_abort()
                try:
                    result = await self._execute(call, obs)
                except AbortRequested:
                    raise
                except Exception as e:
                    result = f"error: {e}"
                    log.warning("tool %s failed: %s", call.name, e)
                if result == "FINISH":
                    ans = str(call.arguments.get("answer", "Done.")).strip()
                    # Save a recipe on success (§7): the executed actions become
                    # a deterministic macro replayed on the next similar request.
                    if getattr(self, "_recipe_actions", None) and actions_done >= 1:
                        from .recipes import RecipeStore

                        RecipeStore(root=getattr(self, "recipe_root", None)).add(
                            self._recipe_request, self._recipe_actions)
                    return ans
                actions_done += 1
                # record for the recipe cache (§7); cap length
                if call.name in ("click_element", "click", "key", "type_text", "bash",
                                 "double_click", "right_click", "scroll", "drag"):
                    self._recipe_actions.append({"tool": call.name,
                                                  "args": dict(call.arguments)})
                    self._recipe_actions = self._recipe_actions[-30:]
                history.append({"role": "assistant", "content": None,
                                "tool_calls": [{"id": call.id or f"c{actions_done}",
                                                "type": "function",
                                                "function": {"name": call.name,
                                                             "arguments": json.dumps(call.arguments)}}]})
                history.append({"role": "tool",
                                "tool_call_id": call.id or f"c{actions_done}",
                                "content": result[:400]})

            if no_change >= NO_CHANGE_LIMIT:
                return "I'm stuck, can you help me?"
            if actions_done >= self.cfg.llm.max_steps:
                answer = "I've hit my action limit for this request."
                break

        return answer or "I couldn't complete that request."
