# Architecture

Two processes, two sockets, one pluggable pipeline. Everything here exists
because of a hard constraint: **the venv that runs the ML stack has no
PyGObject, and the system Python that has GTK4 cannot see the venv's packages.**

```
┌─────────────────────────────── systemd --user: alpha.service ──────────────────────────────┐
│                                                                                            │
│  ┌────────────────────────── daemon (venv python 3.11) ──────────────────────────┐         │
│  │  alpha/daemon.py        state machine + orchestration                          │         │
│  │  alpha/audio.py         SoundDevice mic/speaker, ack/error/done chimes         │         │
│  │  alpha/wake.py          wake matcher (openWakeWord  OR  whisper-tiny KWS)      │         │
│  │  alpha/listen.py        VAD-gated recorder (silero via openwakeword)           │         │
│  │  alpha/stt.py           faster-whisper (int8, auto tiny/small)                 │         │
│  │  alpha/tts.py           piper (en_US-lessac + ar_JO-kareem), Arabic autodetect │         │
│  │  alpha/input/           evdev+uinput (native) | x11 (xdotool) | detect         │         │
│  │  alpha/safety/guard.py  abort flag, allowlist, denylist, audit log             │         │
│  │  alpha/brain/           prompts, tools, loop, recipes, blender, providers/     │         │
│  │  alpha/vision/          facade: request/response, coordinate math, SoM         │         │
│  │  alpha/ipc.py           LineServer (push) + CtlServer (commands)               │         │
│  └───────┬───────────────────────────────────────────────────────────┬────────────┘         │
│          │ hud.sock  (0600, daemon → HUD: state, mute, req{op,id})   │ ctl.sock (0600)      │
│          ▼                                                           ▼                      │
│  ┌──────────────── desktop worker / HUD (/usr/bin/python3, GTK4) ────────────────┐          │
│  │  alpha/hud/app.py      overlay window + tray icon + worker request handler    │          │
│  │  alpha/hud/worker.py   portal ScreenCast + GStreamer pipewiresrc              │          │
│  │                        atspi_focused_tree()                                   │          │
│  └───────────────────────────────────────────────────────────────────────────────┘          │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Why the HUD is a child process

Three hard facts forced this design:

1. GTK4, `Atspi` and GStreamer are only installed for the **system** Python
   (3.14 here). Installing PyGObject in the project venv is fragile and would
   need compiler toolchains.
2. The ML stack (faster-whisper, piper, openwakeword) pins **Python
   3.11/3.12** — `tflite-runtime` has no cp313+ wheels, which is why
   `pyproject.toml` declares `requires-python = ">=3.11,<3.12"`.
3. The tray icon, the overlay and the screenshot must all live in the **user's
   graphical session**, which the venv process also belongs to.

So the daemon owns all logic and the HUD is a dumb, restartable worker. If the
HUD crashes, the daemon keeps working (wake/STT/TTS/input) and re-spawns it.

## IPC

Two unix sockets in `~/.local/state/alpha/`, both `0600`:

| Socket | Direction | Payload |
|---|---|---|
| `hud.sock` | daemon → HUD (broadcast) | `{"type":"state","state":"acting","text":"…"}`, `{"type":"mute","muted":true}`, `{"type":"req","id":7,"op":"screenshot"\|"atspi"\|"screen-init"}` |
| `ctl.sock` | CLI / HUD → daemon | commands: `state mute unmute mute-toggle abort set-geom warm unload res vision-test ask` |

Request/response uses **correlation ids**: the daemon registers a future,
broadcasts a `req`, and the HUD replies on `ctl.sock` with
`{"cmd":"res","id":7,…}` which `DesktopWorker.resolve()` matches. Timeouts are
explicit (`screenshot` 15 s, `atspi` 8 s) and degrade to "no vision" rather than
hanging the request.

## Daemon state machine

```
IDLE ──wake──► LISTENING ──speech+silence──► TRANSCRIBING ──► ACTING ──► SPEAKING ──► IDLE
  ▲                │                                            │           │
  └──── MUTED ◄────┘ (mic genuinely released)                    └── abort ──┘
```

- `IDLE` holds only the wake matcher: whisper-tiny (KWS mode) or a tiny
  openWakeWord model. Everything heavier is unloaded after `idle_unload_s`.
- `MUTED` closes the `sounddevice` stream — the microphone LED goes out. This is
  verified behaviour, not a flag.
- `ACTING` is where the agent loop runs, with abort checks between steps.

## Module map

| Path | Responsibility |
|---|---|
| `alpha/config.py` | dataclass config, strict validation, `config.toml` parsing with defaults |
| `alpha/credentials.py` | discovers providers in `~/.config/opencode/opencode.json`, imports keys into the keyring |
| `alpha/paths.py` | XDG dirs (`~/.config/alpha`, `~/.local/state/alpha`, `~/.local/share/alpha`) |
| `alpha/log.py` | logging with **secret redaction** (API keys never reach the journal) |
| `alpha/models.py` | one-time downloads with progress; whisper, piper voices, openWakeWord models |
| `alpha/doctor.py` | environment diagnostics reported in plain language |
| `alpha/brain/loop.py` | planner + executor: observe → plan → act → verify, 25-step cap |
| `alpha/brain/tools.py` | provider-neutral JSON tool schemas (`click`, `type_text`, `key`, `bash`, `wait`, `finish`, …) |
| `alpha/brain/providers/` | `openai_compatible` (default), `anthropic`, `ollama`, `base`, `FallbackChain` |
| `alpha/brain/recipes.py` | fuzzy-matched macro replay *before* any LLM call |
| `alpha/brain/blender.py` | detects Blender requests and runs a `bpy` script headless instead of clicking the UI |
| `alpha/brain/prompts.py` | the system prompt: environment, grounding mode, rules, blindness disclosure |
| `alpha/input/` | `evdev` (ioctl/key tables), `uinput` (devices), `x11` (xdotool), `detect` |
| `alpha/safety/guard.py` | abort, audit, allowlist/denylist, destructive detection, cost guard |
| `alpha/vision/` | `DesktopWorker` (socket client), coordinate math, Set-of-Mark renderers |
| `alpha/hud/` | the GTK4 process: overlay UI, tray, `ScreenCast`, `atspi_focused_tree` |
| `alpha/ipc.py` | socket servers/clients, `spawn_hud()`, `ctl_client()` |

## Request lifecycle (what actually happens)

1. Wake matcher fires → `audio.ack()` chime → HUD shows `LISTENING`.
2. `Recorder` captures with silero VAD; silence ends the turn.
3. `Transcriber.transcribe()` (whisper, int8) → text; a leading wake phrase is
   stripped if you said it all in one breath.
4. `_run_request(text)`:
   - clear any stale abort, transition to `ACTING`, HUD shows the request;
   - `guard.check_action` gates every action, audit-logged *before* execution;
   - `AgentLoop.run()`:
     - password-field check,
     - daily cost cap check,
     - **recipe fuzzy match** → replay macro and return (no LLM call),
     - **Blender request** → generate + run `bpy` script headless,
     - otherwise: observe (screenshot if the model can see, always AT-SPI) →
       plan → act → verify, up to 25 steps;
5. `SPEAKING`: piper synthesises (Arabic auto-detected by script) and plays.
6. Back to `IDLE`; models are freed after `idle_unload_s` (with `malloc_trim`).

## Testing strategy

- **Pure logic is unit-tested without a desktop**: coordinate math, guard rules,
  config validation, IPC framing, provider parsing, recipe matching, prompt
  invariants (53 tests, `uv run pytest -q`).
- **Live checks are separate scripts** so they never gate CI:
  `scripts/test-input.py` (GTK4 probe), `scripts/m2-audio-test.py`,
  `scripts/soak.py` (memory/CPU).
- The agent loop's tool layer is exercised with mocked subprocess calls, so a
  test can never launch a real browser (that happened once — it opened Firefox
  during a test run; `tests/test_agent_loop.py` now mocks it).
