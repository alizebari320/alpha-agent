# Alpha — voice-driven computer-use agent

> **⚠️ SECURITY & PRIVACY DISCLOSURE — READ THIS FIRST**
>
> Alpha controls your real computer: it moves your mouse, clicks, types, and
> runs commands on your behalf. When you use a cloud LLM provider, **screenshots
> of your screen are sent off your machine** to that provider. By default Alpha
> imports the LLM provider you already use in `opencode` (AgentRouter + GLM),
> but you may configure a fully-local provider (Ollama) or a different one.
>
> - Your API key is stored in your **system keyring**, never in a file or in git.
> - Alpha never phones home, and there is **no telemetry**. The only network
>   calls are to the LLM provider you configure and one-time model downloads.
> - `Ctrl+Alt+Q` aborts any running action loop instantly.
> - Review `config.toml` before you trust it with anything sensitive.

Alpha is a background daemon for Fedora Linux that:

1. Listens offline for a wake word ("Hey Alpha" by default, renameable).
2. On wake: plays an ack sound and shows a floating overlay on top of all apps.
3. Records your request, ends on silence, transcribes it locally.
4. Plans and executes on your real desktop — mouse, keyboard, screen reading.
5. Speaks the answer with local TTS and shows it in the overlay.

## Status

**Milestone 1** (repo scaffold, config + wizard, credential import, keyring,
logging, systemd unit, installer, `alpha doctor`).

| Component | Status |
|-----------|--------|
| Hello-world state machine (`idle`) | ✅ |
| First-run wizard + credential import | ✅ |
| Keyring storage (libsecret) | ✅ |
| systemd `--user` unit + installer | ✅ |
| `alpha doctor` | ✅ |
| Wake word + audio | 🔜 M2 |
| HUD overlay | 🔜 M3 |
| Input injection | 🔜 M4 |
| Vision + AT-SPI + SoM | 🔜 M5 |
| Planner/executor brain | 🔜 M6 |
| Recipe cache + Blender | 🔜 M7 |

## Requirements

- Fedora Linux, Python 3.11+ (the project pins 3.12 via `uv` for ML wheel
  compatibility), `uv`.
- A microphone and speakers.
- For Vision/LLM: an OpenAI-compatible, Anthropic, or Ollama provider.

## Install

```bash
git clone https://github.com/alizebari320/alpha-agent
cd alpha-agent
./scripts/install-fedora.sh
```

The installer imports your existing `opencode` credentials automatically, so the
assistant works with **zero manual key entry**. Then:

```bash
alpha doctor                 # one-command diagnostics
systemctl --user status alpha
journalctl --user -u alpha -f
```

## Usage

- `alpha` — run the daemon in the foreground (what systemd runs; useful for logs).
- `alpha doctor [--no-network]` — diagnostics.
- `alpha init` — (re)run the first-run wizard, re-import credentials, change name.
- `alpha --version`

Future milestones add `alpha recipes list|delete|export` and friends.

## Configuration

`~/.config/alpha/config.toml` — see [`config.toml.example`](config.toml.example)
for every option. `alpha doctor` validates it.

## Repository layout

```
alpha/
  __main__.py   CLI entry (daemon / doctor / init)
  config.py     config.toml load/validate/defaults
  credentials.py opencode discovery + keyring import
  daemon.py     IDLE->LISTENING->THINKING->ACTING->SPEAKING state machine
  log.py        secret-redacting logging
  wake.py       wake word (M2)
  stt.py     tts.py     (M2)
  hud/          overlay (M3)
  input/        X11 + uinput backends (M4)
  vision/       capture + AT-SPI + SoM (M5)
  brain/        provider abstraction + loop (M6)
  safety/       guard (M4)
  doctor.py     alpha doctor
```

See `docs/architecture.md` (populated as milestones land).

## License

Apache-2.0. See [LICENSE](LICENSE). Bundled ML model weights carry their own
licenses — see NOTICE (added when models ship in M2+).