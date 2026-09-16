# Installing Alpha on Fedora

Tested on **Fedora 44 Workstation, GNOME 49, Wayland, Python 3.11**. The
supported target is Fedora 40+; the code avoids anything Fedora-specific except
the package names in the installer.

## TL;DR

```bash
git clone https://github.com/alizebari320/alpha-agent
cd alpha-agent
./scripts/install-fedora.sh     # prints every dnf command and asks before running it
alpha doctor                    # verifies the environment, tells you what is missing
alpha init                      # imports an existing key, or prompts for one
alpha models                    # one-time model download (~250 MB)
systemctl --user enable --now alpha
alpha install-hotkeys
```

## What the installer does, and why each part is needed

The installer never runs anything silently: it prints the full command and asks
first. If you would rather do it by hand, everything it does is listed here.

### 1. Python 3.11

`pyproject.toml` pins `requires-python = ">=3.11,<3.12"`, and this is not
laziness — `tflite-runtime` (used by the bundled wake-word engine) has no
cp313 wheels, and `faster-whisper` + `piper` are happiest on 3.11. Fedora 44
ships Python 3.14 as the system interpreter, so the installer uses `uv`, which
downloads its own 3.11:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.11 .venv
uv pip install -e ".[dev]"
```

> **Do not replace your system `python3`.** Alpha needs the *system* interpreter
> (with PyGObject, GTK4, Atspi, GStreamer) for the HUD process, and the *venv*
> 3.11 for the daemon. That split is deliberate — see
> [ARCHITECTURE.md](ARCHITECTURE.md#two-python-interpreters-on-purpose).

### 2. System packages

```bash
sudo dnf install -y \
  python3-gobject gtk4 libadwaita at-spi2-core \
  pipewire pipewire-gstreamer gstreamer1-plugins-base gstreamer1-plugins-good \
  xdg-desktop-portal xdg-desktop-portal-gnome \
  wmctrl xdotool portaudio libnotify
```

| Package | Why Alpha needs it |
|---|---|
| `python3-gobject`, `gtk4`, `libadwaita` | the HUD overlay (system python child process) |
| `at-spi2-core` | the accessibility element tree — Alpha's text-mode "eyes" |
| `xdg-desktop-portal`, `-gnome` | ScreenCast portal, the only sanctioned way to get a screenshot on Wayland |
| `gstreamer1-plugins-*`, `pipewire-gstreamer` | decoding the portal's PipeWire video stream |
| `pipewire` | audio I/O (recording and playback) |
| `wmctrl`, `xdotool` | window focus / listing fallbacks |
| `portaudio` | the `sounddevice` backend |
| `libnotify` | desktop notifications for errors |

### 3. Permissions

Only one thing is needed, and it is a **group membership**, not sudo:

```bash
sudo usermod -aG input "$USER"      # lets Alpha open /dev/uinput
```

Log out and back in (or `newgrp input`) for it to apply. Alpha writes to
`/dev/uinput` via a dedicated pair of virtual devices; it does **not** need root,
and it does **not** use `/dev/input/event*` for injection. Verify with:

```bash
alpha doctor          # looks for "input backend: uinput (writable)"
```

`udev` rules are only needed if your distribution restricts `/dev/uinput` more
than Fedora does; the installer will print the rule and explain it if it detects
that case.

### 4. The systemd user unit

```bash
alpha install-service       # renders the unit from your config, then:
systemctl --user daemon-reload
systemctl --user enable --now alpha
```

`alpha install-service` is worth understanding: it generates
`~/.config/systemd/user/alpha.service` **from your `config.toml`**, including
`MemoryMax`. If you later change `resources.memory_max_mb`, re-run it —
otherwise `alpha doctor` will report that the deployed unit has drifted from
your config.

Useful knobs in the unit:

```ini
MemoryMax=2048M      # measured warm need is ~1176M (daemon + HUD in one cgroup)
Restart=on-failure
After=graphical-session.target
```

The service is `--user` and starts at login, which is what you want: a computer-use
agent must run inside your graphical session to have a session bus, a portal and
a display.

### 5. Models

```bash
alpha models
```

Downloads once, into `~/.local/share/alpha/models/`:

| Model | Size | Used for |
|---|---|---|
| `whisper-small` (or `tiny.en` under 8 GB RAM) | ~500 MB | speech recognition |
| `whisper-tiny` | ~75 MB | wake-word mode `kws`, only if you use it |
| `piper en_US-lessac-medium` | ~60 MB | speech |
| `piper ar_JO-kareem-medium` | ~60 MB | speech, Arabic-detected replies |

Nothing else is downloaded at runtime. Speech recognition and speech synthesis
are fully offline; the **only** network traffic after setup is the LLM call.

### 6. The provider key

Alpha has no API keys in its source and never writes one to `config.toml` — keys
live in the desktop keyring (libsecret).

```bash
alpha init
```

It will (a) look for an existing opencode/OpenAI-compatible config on your
machine and offer to import the key, or (b) prompt for a key and store it in the
keyring. Check it with `alpha doctor`, which lists the models the provider
actually offers and whether they accept images.

## Verifying the install

```bash
alpha doctor                 # environment, groups, models, provider, vision layer
alpha ask --no-speak "what is 2 plus 2"
```

The second command runs the real pipeline (planner → tool loop → answer) with no
microphone and no speech. If it answers, the brain works. Then try the voice path.

## Uninstalling

```bash
systemctl --user disable --now alpha
rm -f ~/.config/systemd/user/alpha.service
rm -rf ~/alpha-agent ~/.local/share/alpha          # models, recipes
rm -rf ~/.local/state/alpha ~/.config/alpha        # logs, sockets, audit trail, config
```

Remove yourself from the `input` group only if nothing else needs it:
`sudo gpasswd -d "$USER" input`.

## Next

- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — when something does not work.
- [PERFORMANCE.md](PERFORMANCE.md) — what it costs to keep running.
- [SAFETY.md](SAFETY.md) — read this before you let it click things.
