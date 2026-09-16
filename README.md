# Alpha — voice-driven computer-use agent

> **⚠️ SECURITY & PRIVACY DISCLOSURE — READ THIS FIRST**
>
> Alpha controls your real computer: it moves your mouse, clicks, types, and
> runs commands on your behalf. When you use a cloud LLM provider, **screenshots
> of your screen are sent off your machine** to that provider. Alpha imports the
> LLM provider you already use in `opencode` (AgentRouter, TokenHarbor, …), but
> you may configure a fully-local provider (Ollama) or a different one.
>
> - Your API key is stored in your **system keyring**, never in a file or in git.
> - Alpha never phones home, and there is **no telemetry**. The only network
>   calls are to the LLM provider you configure and one-time model downloads.
> - `Ctrl+Alt+Q` aborts any running action loop instantly.
> - Every action is written to `~/.local/state/alpha/actions.jsonl` **before** it
>   executes, so a crash mid-action still leaves a trail.
> - Review `~/.config/alpha/config.toml` before you trust it with anything
>   sensitive. The `denylist` and `bash_allowlist` are the controls you want.

Alpha is a background daemon for Fedora Linux that:

1. Listens offline for a wake word ("Hey Alpha" by default, renameable).
2. On wake: plays an ack sound and shows a floating overlay on top of all apps.
3. Records your request, ends on silence, transcribes it locally (faster-whisper).
4. Plans and executes on your real desktop — mouse, keyboard, screen reading.
5. Speaks the answer with local TTS (piper, English + Arabic) and shows it in the overlay.

Everything except the LLM call runs on your machine. Speech in, speech out, no
audio ever leaves the computer.

---

## Status

All eight milestones are implemented. Status is stated exactly as measured — see
[docs/PERFORMANCE.md](docs/PERFORMANCE.md) for the raw numbers and
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for the known limits.

| # | Milestone | Status |
|---|-----------|--------|
| M1 | Scaffold, config + wizard, credential import, keyring, systemd unit, `doctor` | ✅ done |
| M2 | Wake word, VAD recording, STT, TTS, model downloads, idle unload | ✅ done |
| M3 | GTK4 HUD overlay, state colours, mute, tray icon, wake/mute sounds | ✅ done |
| M4 | Input injection (uinput absolute pointer + keyboard), abort hotkey, audit | ✅ done |
| M5 | Screen vision (portal ScreenCast), AT-SPI elements, Set-of-Mark tables | ✅ done¹ |
| M6 | Provider-neutral brain: planner/executor loop, tools, cost guard | ✅ done |
| M7 | Recipes (macro replay), Blender headless API path, multi-monitor math | ✅ done |
| M8 | Soak test + measured RAM table, wake-word training, docs, CI, v0.1.0 | ✅ done |

¹ Vision is implemented and unit-tested, but on GNOME **Wayland** the
screenshot path needs you to approve the portal "Share your screen" dialog
once, and it only helps if your LLM can accept images. Without both, Alpha
degrades gracefully to text-only grounding via the accessibility tree. Read
[docs/VISION.md](docs/VISION.md) before judging it.

**Verified live on this machine** (Fedora 44, GNOME 49 Wayland, Python 3.11):

- Wake word → VAD → whisper transcription → LLM → spoken answer, end to end.
- `alpha ask "…"` drives the identical plan/act/verify path with no microphone.
- uinput injection is **pixel-exact** on Wayland: a GTK4 probe window received
  motion, click, drag, typed text and `ctrl+l` at the coordinates requested.
- **Application launching works**: asked in text mode to open Firefox, the agent
  ran `bash("pgrep -x firefox")` then `bash("firefox")` (both in the audit log)
  and Firefox came up. This was *broken* until the fix in `f4165ec` — a stray
  `import asyncio` inside the executor made every bash call raise
  `UnboundLocalError`, so GUI launching silently failed and the planner just
  retried. Two regression tests now cover it.
- The abort hotkey kills a running action loop within one step.
- 60 unit tests pass (`uv run pytest -q`), ruff clean.

**Not verified here** (stated plainly so you know what you are trusting):

- Screenshot grounding with a real mouse click on a UI element. Injection and
  vision are each tested on their own, but this machine's current free model
  rejects images and the portal share prompt was never approved, so the loop
  has only ever run text-only session-to-session. See
  [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#i-want-the-full-vision-loop).
- The trained custom wake word. The training pipeline is wired up and checked,
  but training it needs a GPU/Colab run you have to kick off yourself.

---

## Quickstart (Fedora)

```bash
git clone https://github.com/alizebari320/alpha-agent && cd alpha-agent
./scripts/install-fedora.sh          # dnf packages, uv, venv, systemd unit (asks first)

cp config.toml.example ~/.config/alpha/config.toml   # or: alpha init
alpha doctor          # checks python, audio, uinput, portal, provider, models
alpha init            # imports your existing opencode provider key into the keyring
alpha models          # one-time download of whisper + piper voices (~250 MB)

systemctl --user enable --now alpha
alpha install-hotkeys # Ctrl+Alt+M mute, Ctrl+Alt+Q abort
```

Then say **“Hey Alpha”**, wait for the chime, and speak your request.

No microphone handy? Use text mode — it runs the exact same pipeline:

```bash
alpha ask "open Firefox and search for red pandas"
alpha ask --no-speak "what is 2 plus 2"     # print only, no TTS
```

Other commands:

```bash
alpha state                   # idle / listening / acting / speaking
alpha mute / unmute / mute-toggle
alpha abort                   # same as Ctrl+Alt+Q
alpha warm / unload           # pre-load or free speech models, shows MiB
alpha recipes list            # learned deterministic macros (M7)
alpha doctor                  # environment + provider diagnostics
```

---

## How it works

```
 mic ──► wake word ──► VAD record ──► faster-whisper ──► ┐
 (offline: openWakeWord or whisper-tiny KWS)            │
                                                        ▼
                              ┌─────────────── planner / executor ───────────────┐
                              │  recipe cache hit? → replay macro, no LLM call   │
                              │  blender request?  → run bpy script headless     │
                              │  otherwise         → LLM tool loop (25 steps max) │
                              └───────┬──────────────────────┬───────────────────┘
                                      ▼                      ▼
                       HUD overlay (GTK4, systemd child)   input backend
                       AT-SPI element table via worker      uinput (absolute
                       portal screenshot (opt-in)           pointer + keyboard)
                                      │                      │
                                      └──────────► audit log before every action
```

Process model: the daemon (venv Python, no PyGObject) orchestrates, and the HUD
runs as a child process under system `/usr/bin/python3` because that is where
GTK4, Atspi and GStreamer live. They talk over two `0600` unix sockets in
`~/.local/state/alpha/`. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Measured resources

Real numbers from `scripts/soak.py` on a 16-core / 16 GB Fedora 44 machine
(full log: [docs/PERFORMANCE.md](docs/PERFORMANCE.md)):

| Phase | daemon | HUD | total | CPU |
|---|---|---|---|---|
| Idle, `wake_word.mode="pretrained"` | 205 MiB | 179 MiB | **384 MiB** | 5.9 % |
| Idle, `wake_word.mode="kws"` (default) | 292–393 MiB | 179 MiB | **465–573 MiB** | **2.2 %** |
| After `alpha warm` (whisper-small int8 + piper) | 1003 MiB | 173 MiB | **1176 MiB** | 2.2 % |
| After the idle unload (`idle_unload_s`) | 292 MiB | 173 MiB | **466 MiB** | 2.2 % |

No leaks: a 3-minute soak showed +0.0 MiB RSS drift, +0 fds, +0 threads.
Speech models are loaded on demand and **genuinely** released after
`idle_unload_s` — that is why Alpha calls `malloc_trim`; without it, freed
arenas stay in RSS (measured: 612 MiB before *and* after `unload`, and
1003 → 292 MiB with it).

The warm figure is why `memory_max_mb` defaults to 2048: a warm daemon plus the
HUD is 1176 MiB in one cgroup, so the original 1024 MB limit would have had
systemd OOM-kill Alpha mid-request. The HUD's 179 MiB is the GTK4/NVIDIA/Mesa
stack (`libnvidia-gpucomp`, `libLLVM`, GTK caches), not Alpha's own code.

Full method, latency table and the wake-mode trade-off: [docs/PERFORMANCE.md](docs/PERFORMANCE.md).

---

## Safety model

| Control | Behaviour |
|---|---|
| `Ctrl+Alt+Q` / `alpha abort` | Sets the abort flag; checked between every step and before every action. |
| Audit log | `~/.local/state/alpha/actions.jsonl`, appended **before** execution. |
| `bash_allowlist` | App launches run without asking. Read-only checks (`pgrep`, `wmctrl -l`, `xdotool search`, `gsettings get`) are always allowed as whole compound commands **only if every segment is safe**. |
| Destructive regex | `sudo`, `rm`, `dd`, `mkfs`, `shutdown`, `git push --force`, `gsettings reset`… always require a spoken yes. |
| Output redirects | Writing to a real file is never allowlisted (`echo x > ~/.bashrc` asks). |
| `denylist` | Named apps are refused outright, never confirmed. |
| Password lock | If the accessibility tree reports a focused password field, Alpha refuses to type or click. |
| Cost guard | Per-day USD cap and per-request step cap; spend appended to `spend.jsonl`. |

Full write-up: [docs/SAFETY.md](docs/SAFETY.md).

---

## Documentation

| Doc | What it covers |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Fedora install, permissions, groups, systemd, hotkeys, uninstall |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Process model, sockets, module map, data flow |
| [docs/INPUT.md](docs/INPUT.md) | Input injection: why uinput, absolute pointer, Mutter timing, X11 vs Wayland |
| [docs/VISION.md](docs/VISION.md) | Portal screen sharing, coordinate math, AT-SPI, Set-of-Mark |
| [docs/SAFETY.md](docs/SAFETY.md) | Abort, audit, allowlist, denylist, password lock, cost guard |
| [docs/WAKE_WORD.md](docs/WAKE_WORD.md) | Renaming the wake word, training a custom model, Colab notebook |
| [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | Soak-test method, measured RAM/CPU/latency tables |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Every failure we actually hit, and what to do |
| [docs/DEMO.md](docs/DEMO.md) | How to record a demo, and what the tests already prove |

---

## Known limits (stated honestly)

- **GNOME Wayland + HUD on top:** Mutter does not implement
  `zwlr_layer_shell_v1`, so a GTK4 overlay cannot be pinned above fullscreen
  apps. The HUD uses `gtk4-layer-shell` when the compositor supports it and
  warns otherwise. For a guaranteed always-on-top HUD, log into *GNOME on Xorg*
  — the input backend works identically there.
- **Screenshots need one-time consent.** The portal dialog must be approved
  (choose "Share" and ideally "Always allow"), otherwise Alpha runs text-only.
- **Vision needs a vision-capable model.** Alpha probes this automatically
  (a 1-pixel image) and logs `vision auto-probe: False` when the model refuses
  images; it then grounds on the accessibility tree instead of guessing pixels.
- **LLM latency dominates.** With the free providers this test used, a request
  takes tens of seconds; a paid/faster model is a config change
  (`llm.planner.model`), not a code change.
- **Blender path is untested on a machine without Blender** — the API-first
  script generator is unit-tested, but `blender` was not installed here, so
  Alpha reports "Blender isn't installed" rather than pretending.

## License & contributing

Apache-2.0 — see [LICENSE](LICENSE). Issues and PRs welcome; please run
`uv run ruff check . && uv run pytest -q` first. See [CHANGELOG.md](CHANGELOG.md).
