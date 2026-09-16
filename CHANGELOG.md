# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **The abort path was dead while Alpha was acting** — the most serious bug
  found so far. `provider.send()` is a synchronous httpx call and it ran inline
  in the daemon's event loop, so for the whole of every model round-trip (up to
  120 s) the control socket could not be served: `alpha state`, `alpha mute` and
  **`alpha abort`** all died with *daemon not reachable: timed out*. Ctrl+Alt+Q
  arrives over that same socket, so the global abort — the one thing the spec
  insists must work before any input is injected — was dead exactly while Alpha
  was touching the desktop. Both model calls now run via `asyncio.to_thread`;
  the event loop keeps turning and the abort lands mid-request. Regression test
  is a liveness probe (a heartbeat must keep beating while a deliberately slow
  provider is in flight): **0 beats** before the fix, passing after.
  Verified live: while a request was in flight, `alpha state` answered
  `state=acting` on six consecutive polls, and `alpha abort` mid-request
  returned *aborted*, drove the daemon back to `idle` and logged
  `ABORT requested (ctl)` — with the in-flight request answering "Aborted."
  instead of running its queued action.
- `error: busy` is now *"Alpha is already working on another request — wait for
  it to finish, or run `alpha abort` first"* (§11.5).

## [0.1.1] - 2026-09-16

### Fixed

- **CI never actually ran.** Every run died before a single check: `setup-uv`'s
  `python-version` input creates and activates `.venv` itself, so the workflow's
  own `uv venv` failed with *Failed to create virtual environment* (exit 2). The
  interpreter is now installed explicitly and the venv is ours.
- **The `dev` extra was not an extra.** pytest/ruff sat in a uv-only
  `[dependency-groups]` table, so `uv pip install -e ".[dev]"` — the exact
  command `docs/INSTALL.md` gives contributors — installed nothing and warned
  *does not have an extra named 'dev'*, after which CI's `.venv/bin/ruff` was
  not found (exit 127). Moved to `[project.optional-dependencies]`, which works
  with uv and plain pip alike.
- **The unit tests read the developer's real `~/.config/alpha/config.toml`.**
  They passed on the development machine only because that file exists there,
  and failed on a clean runner with `ConfigError`. `unit.render()` now takes an
  injectable config and only reads disk when it must; verified with `HOME` set to
  an empty directory.
- **Idle CPU in `kws` mode** — the wake gate let room noise through, so
  whisper-tiny was asked to transcribe noise roughly every 12 s (31 times in one
  6-minute stretch, answering every one with its classic hallucination set:
  `'Thank you.'`, `'I'm sorry.'`) at a cost of ~0.65 s of CPU each. The gate now
  tracks room tone (`listen.NoiseFloor`, 10th percentile of the last ~1 minute of
  windows) and requires a window to clear 2x that *and* the absolute `min_rms`.
  Measured on the same machine, room and 6-minute soak: idle CPU **11.6 % -> 1.7 %**
  of a core, idle daemon RSS **342 MiB -> 194 MiB** (whisper-tiny now never loads
  at all), peak idle CPU **18 % -> 2.4 %**. Every gate opening is logged at INFO.
- Each `kws` decode costs ~0.65 s of CPU regardless of window length (whisper pads
  to a 30 s mel chunk), so the gate *is* the CPU budget. Measured: 2.0 s -> 653 ms,
  1.0 s -> 606 ms, 0.5 s -> 567 ms; and the background decoder is pinned to one
  thread (112.9 % -> 90.7 % for the same workload, leaving other cores free).

Found by re-walking every documented flow end to end after 0.1.0. The first
three are the same bug wearing three hats: a trained wake word could be
*installed* but never *used*, and the docs described a config key that does not
exist.

### Fixed

- **A trained wake word was unusable, and the install went to the wrong place.**
  `scripts/train-wake-word.py --install` copied the model into
  `~/.local/share/alpha/models/`, but config validation **and** the loader
  resolve custom wake words in `~/.local/share/alpha/wakewords/`. On top of
  that, `PretrainedWake` rejected any name outside `BUNDLED_WAKEWORDS` even when
  the file existed. So the documented train → `--install` → set
  `mode = "pretrained"` flow passed nothing and crashed the daemon at startup
  with `ValueError: 'hey_alpha' has no shipped pretrained model`. The installer
  now targets the directory the loader reads, `resolve_pretrained_model()`
  returns openWakeWord a *path* for trained models (falling back to the bundled
  name, and matching the inference framework to the file it found), and the
  docs point at `wakewords/` too.
- **`--check` and `--run` hard-coded
  `.venv/lib/python3.11/site-packages/openwakeword/train.py`**, so the training
  driver only worked from a checkout with that exact layout. It now asks the
  installed package where `train.py` is and prints the resolved path.
- **Misspelled config keys were silently ignored.** `docs/WAKE_WORD.md`
  documented `wake_word.min_score` in four places; the field is `threshold`, so
  following the docs to tune sensitivity did nothing at all, with no warning.
  Unknown keys are now reported — with a did-you-mean suggestion and the list of
  valid keys — by `alpha doctor` and in the daemon log. The docs are corrected.
- **`alpha config` errors pointed at a script that does not exist**
  (`scripts/train-wakeword.py`).

### Changed

- **`kws` wake mode no longer runs whisper on every sound.** Each decode costs a
  *fixed* ~0.65 s of CPU — whisper pads every input to a 30 s mel chunk, so a
  0.5 s window costs 567 ms and a 2 s window costs 653 ms (both measured). Idle
  in a quiet room is ~2 % of one core, but continuous background speech pushed
  the same daemon to 20–85 % spikes. Three changes: the wake gate is now a
  single call that checks the VAD ratio *and* an RMS floor, the new
  `assistant.wake_word.min_rms` (default **300**, int16 units) makes that floor
  tunable, and the background decoder is capped at one CPU thread
  (`cpu_threads=1`; the request transcriber still uses every core).
- `alpha doctor --levels` samples the microphone and prints, per window, the
  level and VAD ratio next to the thresholds and whether the window would be
  decoded — so the floor can be tuned against your room instead of guessed.
- `scripts/soak.py` drives both wake→warm and unload transitions itself and
  reports a separate `cooled` phase. Previously the warm phase never ended, so
  731 MiB (warm), 215 MiB (unloaded) and 377 MiB (after the wake path's first
  decode) were averaged together under one label.

### Measured (same machine as below)

| Phase (`kws` default) | daemon | HUD | total | CPU |
|---|---|---|---|---|
| Fresh start, wake matcher loaded, nothing decoded yet | 172 MiB | 179 MiB | 351 MiB | 1.2 % |
| Idle after the wake path's first transcription | 342 MiB | 179 MiB | 521 MiB | 1.2 % |
| After `alpha warm` (whisper-small + piper + **kws whisper-tiny**) | 985 MiB | 179 MiB | 1165 MiB | 2.2 % |
| After `alpha unload` | 215 MiB | 179 MiB | 394 MiB | 2.2 % |
| Idle while the room is talking | — | — | — | up to 85 % spikes |

## [0.1.0] — 2025-09-16

First release. A voice-driven computer-use agent for Fedora that runs as a
user service, on both Wayland and X11, without root.

### Added

**M1 — foundation**
- `alpha` CLI: `doctor`, `init`, `models`, `install-service`, `state`, `mute`,
  `unmute`, `mute-toggle`, `abort`, `install-hotkeys`, `ask`, `warm`, `unload`,
  `vision-test`, `recipes`.
- Validated `config.toml` (every value checked, with actionable errors) at
  `~/.config/alpha/config.toml`.
- Credential import: discovers an existing opencode/OpenAI-compatible config and
  moves the key into the desktop keyring (libsecret). Keys never enter the repo,
  the config file, or logs — the logger redacts `sk-…` patterns.
- systemd `--user` unit, rendered from the config by `alpha install-service`
  so that `resources.memory_max_mb` is genuinely mirrored into `MemoryMax`.
- `alpha doctor`: Python, groups, `/dev/uinput`, audio devices, models, provider
  reachability, available models, vision-capability probe, unit drift, memory.

**M2 — speech**
- Wake word: openWakeWord `pretrained` mode (stock models) and `kws` mode
  (rolling VAD-gated 2 s window transcribed by whisper-tiny, with `ph → f`
  phonetic normalisation and per-word fuzzy matching) — because openWakeWord
  ships no "hey alpha" model.
- VAD-gated recording (silero via openwakeword, energy fallback).
- `faster-whisper` int8 speech recognition, `tiny.en` under 8 GB RAM and `small`
  above, language auto-detected.
- `piper` speech synthesis with English and Arabic voices, Arabic replies spoken
  in Arabic.
- One-time model downloads with progress; idle unload after `idle_unload_s`.

**M3 — HUD**
- GTK4 overlay as a systemd child process, running under system `/usr/bin/python3`
  because that is where GTK4/PyGObject/Atspi/GStreamer live.
- State colours, mute indication, tray icon, distinct wake/ack/error/done sounds.
- Two mode-`0600` unix sockets: daemon→HUD push and a control socket.
- Mute **releases the microphone** rather than discarding samples.

**M4 — input**
- `uinput` backend creating two clean virtual devices (`alpha-agent pointer`,
  `alpha-agent keyboard`) as an **absolute** pointer with `INPUT_PROP_POINTER`,
  so libinput/Mutter applies no pointer acceleration and coordinates are
  pixel-exact on Wayland.
- Readiness handshake (`READY_SETTLE_S`): Mutter adds hotplugged devices
  asynchronously and silently drops events sent in the first ~2.5 s.
- X11 backend (`xdotool`) and a detector that picks the right one.
- Abort (`Ctrl+Alt+Q`) and mute (`Ctrl+Alt+M`) hotkeys; the abort flag is created
  before any input device exists, so it can always fire.
- Audit log `~/.local/state/alpha/actions.jsonl`, written **before** execution.

**M5 — vision**
- Portal `ScreenCast` screenshots via GStreamer `pipewiresrc`, inside the HUD
  process, on request/response with correlation ids.
- AT-SPI focused-window element tree (the text-mode "eyes") with password-field
  detection.
- Coordinate math (downscale, model↔screen, monitor clipping) and Set-of-Mark
  labels/tables/annotation rendering.

**M6 — brain**
- Provider-neutral planner/executor loop: OpenAI-compatible, Anthropic, Ollama,
  with a fallback chain.
- 25-step cap, abort checks between every step, screenshot-hash verification of
  actions, `CostGuard` with a per-day USD cap and `spend.jsonl`.
- Human-readable provider errors (quota, unavailable model with the model list,
  gateway HTML retried, raw body dumpable via `ALPHA_DEBUG_PAYLOAD`).
- Guard: allowlist, read-only command families, destructive regex, denylist,
  output-redirect rejection, spoken confirmation for the rest.

**M7 — recipes and Blender**
- Fuzzy-matched deterministic macro replay *before* the LLM is consulted, so a
  repeated request costs nothing and cannot drift.
- Blender path that prefers the `bpy` API headless over GUI clicking, with a
  bundled procedural template.
- `alpha recipes list|delete|export|import`.

**M8 — hardening, measurement, docs**
- Soak harness (`scripts/soak.py`) sampling RSS/CPU/threads/fds from `/proc`,
  with a leak check; results in [docs/PERFORMANCE.md](docs/PERFORMANCE.md).
- Wake-word training driver (`scripts/train-wake-word.py`) around openWakeWord's
  official `train.py`, with `--check` and `--print-config`, plus a Colab notebook.
- Documentation: INSTALL, ARCHITECTURE, SAFETY, VISION, INPUT, WAKE_WORD,
  PERFORMANCE, TROUBLESHOOTING, DEMO.
- CI: ruff + pytest on Python 3.11.

### Fixed

- **The `bash` tool never worked.** A redundant `import asyncio` inside
  `AgentLoop._execute` made `asyncio` function-local for the whole function, so
  the `bash` branch's `asyncio.to_thread` raised `UnboundLocalError` on every
  call. Since `bash` is how Alpha launches applications, GUI launching silently
  failed and the planner retried forever. Fixed and covered by two regression
  tests, one of which asserts via AST that no local import shadows a
  module-level name there.
- **`alpha unload` freed nothing visible.** glibc and `ctranslate2` keep freed
  arenas in RSS; `_release_free_memory()` now calls `libc.malloc_trim(0)`
  (measured 1003 MiB → 292 MiB).
- **`MemoryMax` was documented but not applied.** The unit was a static 1024M
  file while the measured warm footprint (daemon + HUD in one cgroup) is
  1176 MiB, so systemd would have OOM-killed Alpha mid-request. The default is
  now 2048M, the unit is rendered from the config, and `doctor` reports drift.
- **`NameError` on the voice path.** `_hud_state(text)` ran before the
  transcription had assigned `text`, so every spoken request crashed.
- **GUI launches blocked the loop** for the full 30 s tool timeout and were then
  reported to the planner as failures, causing relaunches. GUI apps are now
  launched detached (`start_new_session=True`).
- **The guard stalled the loop** by asking for spoken confirmation on read-only
  checks such as `pgrep`. Read-only command families are now allowed when every
  segment of a compound command is safe.
- **Security hole:** `echo hi > /etc/passwd` was allowlisted. Any output
  redirect to a real file now requires confirmation.
- **A missing portal permission was reported as "daemon not reachable"**, which
  sent people debugging sockets instead of clicking Share. `vision-test` now
  distinguishes "the RPC was answered" from "a screenshot was captured".
- **A wasted LLM round trip on every daemon start:** the vision-capability probe
  costs a real API call (~60 s on a free tier). The verdict is now cached in
  `~/.local/state/alpha/vision-capability.json`, keyed by
  provider/model/base_url.
- **The vision probe on a text-only model** now names the model that would work,
  instead of failing opaquely.
- Fire-and-forget tasks (acknowledgement sounds) are held by strong references
  so the event loop cannot garbage-collect them mid-flight.
- Unit tests no longer write the real `~/.local/state/alpha/actions.jsonl`.

### Measured (16 cores, 16 GB RAM, Fedora 44, GNOME 49 Wayland)

| Phase | daemon | HUD | total | CPU |
|---|---|---|---|---|
| Idle, `wake_word.mode="pretrained"` | 205 MiB | 179 MiB | 384 MiB | 5.9 % |
| Idle, `wake_word.mode="kws"` | 172–393 MiB | 179 MiB | 351–573 MiB | 1.2–2.2 % |
| After `alpha warm` | 1003 MiB | 173 MiB | 1176 MiB | 2.2 % |
| After idle unload | 292 MiB | 173 MiB | 466 MiB | 2.2 % |

No leaks over a 3-minute soak: +0.0 MiB RSS, +0 fds, +0 threads.

### Known limits

- On GNOME Wayland, Mutter does not implement `zwlr_layer_shell_v1`, so the HUD
  cannot be pinned above fullscreen windows; GNOME on Xorg guarantees it.
- Screenshots require approving the portal ScreenCast dialog once, and grounding
  on pixels requires a vision-capable model. Otherwise Alpha runs text-only on
  the accessibility tree.
- `kws` wake mode costs meaningfully more RAM/CPU than `pretrained`; it is the
  default only because there is no stock "hey alpha" model.
- Blender support is unit-tested but was not exercised with Blender installed.
- Latency is dominated by the LLM provider: tens of seconds per step on free
  tiers.

[0.1.0]: https://github.com/alizebari320/alpha-agent/releases/tag/v0.1.0
