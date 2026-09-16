# Troubleshooting

Every entry below is a failure that actually happened while building and running
Alpha on Fedora 44 / GNOME Wayland, with the fix that resolved it. Start with:

```bash
alpha doctor                                   # environment + provider + vision layers
systemctl --user status alpha
journalctl --user -u alpha -n 100 --no-pager   # daemon log
tail -20 ~/.local/state/alpha/actions.jsonl    # audit trail (what it tried)
tail -20 ~/.local/state/alpha/logs/hud.log     # the GTK4 process's own log
```

## First: three things that look like bugs but are not

**1. Nothing happens for 30–60 seconds after you speak.**
Free and cheap LLM endpoints are slow. 60 s per planning step has been measured
on a free-tier model. `alpha state` shows the state moving
`listening → acting → speaking`, so you can tell it is working rather than hung.
For interactive use, configure a paid/faster model.

**2. The daemon's memory jumps after the first request and never comes back down.**
This is `ctranslate2` (whisper) and glibc retaining scratch arenas. Measured:
292 MiB idle before the first transcription, 393 MiB after, and it stays there.
It is not a leak — a 30-minute soak showed `+0.0 MiB` drift once settled. See
[PERFORMANCE.md](PERFORMANCE.md).

**3. A line in `actions.jsonl` is not proof the action succeeded.**
By design the audit entry is written **before** execution, so that a crash cannot
lose the record of an attempted action. A logged action may still have failed;
that is what the result line in the journal is for.

---

## The agent does nothing, or says it failed, but the audit log shows an action

Look at what the loop actually returned:

```bash
journalctl --user -u alpha --since "5 min ago" --no-pager | grep -A3 "tool="
```

A tool that always returns `failed: …` will make the planner retry forever.
This has happened once in a way worth knowing about: a stray
`import asyncio` inside the executor made `asyncio` function-local, so **every**
`bash` call raised `UnboundLocalError`, and the planner just kept re-trying as
if the app refused to open. If a whole *category* of tool (all bash, all clicks)
is failing while others work, suspect the code path, not the machine. Two
regression tests now cover that exact class of bug
(`tests/test_agent_loop.py::test_bash_tool_runs` and
`::test_wait_tool_does_not_bind_asyncio_locally`).

## The provider rejects everything

| Message | Cause | Fix |
|---|---|---|
| `401 unauthorized client detected` | the provider's WAF blocks unknown clients | set `user_agent` on the provider role to one it accepts (Alpha defaults to `opencode/1.18.30` for this reason) |
| `insufficient_user_quota` / `insufficient balance` | the account is out of credit | `alpha doctor` lists the models the key can actually reach; switch provider or top up |
| `Model 'x' does not accept image input. Use 'y'` | you enabled vision against a text-only model | either set `vision.enabled = false`, or point the `grounder` role at the vision model it names |
| `provider failed after retries` with HTML in the message | the endpoint returned a gateway page, not JSON | transient; Alpha already retries. To see the exact rejected body: |

```bash
ALPHA_DEBUG_PAYLOAD=1 alpha ask --no-speak "test"
cat ~/.local/state/alpha/rejected-request.json     # the exact request/response
```

## I want the full vision loop

Symptom: the journal says

```
vision: screenshot unavailable — grant screen sharing in the HUD (Share button)
```

This is not an error, it is consent. On Wayland a screenshot is only possible
through the xdg-desktop-portal ScreenCast dialog, and that dialog must be
approved by a human, once per session:

1. Click **Share** in the HUD (or trigger any vision action).
2. The GNOME dialog *"Allow Alpha to share your screen?"* appears — approve it.
3. `alpha vision-test` should now write a PNG you can look at.

If you approve it and still get `None`:

- Check the portal is running: `systemctl --user status xdg-desktop-portal`.
- Check the GNOME implementation: `systemctl --user status xdg-desktop-portal-gnome`.
- Do **not** probe the portal by hand with malformed `gdbus` calls — an
  incomplete `CreateSession` reliably trips an assert inside
  `xdg-desktop-portal` and takes the portal down for your whole session
  (`xdp_session_initable_init: (session->token != NULL)`). Use `alpha vision-test`.

Two further conditions for real grounding, both outside Alpha's control:

- **Your model must accept images.** `alpha doctor` probes this and caches the
  verdict in `~/.local/state/alpha/vision-capability.json`; if it says no, the
  loop runs text-only from the accessibility tree.
- **`AT-SPI` must see a focused window.** `atspi failed: no focused window` is
  normal when nothing has focus (e.g. right after an app launches).
  `alpha doctor` checks the accessibility bus for real.

## The HUD does not appear, or is invisible over a fullscreen app

```bash
tail -50 ~/.local/state/alpha/logs/hud.log
```

The HUD is a GTK4 process running under **system** `/usr/bin/python3`, because
the venv has no PyGObject. If that interpreter lacks GTK4 you get an import
error here and everything else still works (the daemon broadcasts into a socket
nobody is listening to).

On Wayland, GNOME's Mutter does not implement `zwlr_layer_shell_v1`, so a HUD
cannot be layered above fullscreen windows the way it can on X11 or on wlroots.
Alpha deliberately degrades instead of fighting the compositor. If you need a
HUD that is *guaranteed* on top of fullscreen apps, run a **GNOME on Xorg**
session — that is a supported configuration, and the same code path is used.

## Wake word problems

**It fires constantly, or transcribes the TV.**
Check what the input device is:

```bash
alpha doctor | grep -A5 audio
pactl list short sources
```

Opening the *monitor* of an output device as an input makes Alpha hear
everything the machine plays. Pick the real microphone in `config.toml`
(`audio.input_device`).

**It ignores me.**
The default mode is `kws`, which recognises a *phrase*, not a speaker:

```toml
[wake]
mode = "kws"
phrase = "hey alpha"     # this is what it listens for
```

`pretrained` mode uses openWakeWord's stock models — `hey_jarvis`,
`hey_mycroft`, `hey_rhasspy`, `alexa` — **there is no stock "hey alpha"**, which
is why `kws` is the default. To get a real "hey alpha" model with openWakeWord's
own accuracy, train one: [WAKE_WORD.md](WAKE_WORD.md).

Measured cost of each mode on this machine: `kws` ≈ 292–393 MiB and 2.2 % CPU
idle, `pretrained` ≈ 205 MiB and 5.9 % CPU idle. If RAM matters more than the
phrase, use `pretrained`.

**It hears itself speak.**
Mute state genuinely releases the microphone rather than ignoring samples; if
you hear it re-trigger on its own speech it means the wake detector is running
while Alpha speaks, which is the `idle_unload_s` / barge-in interaction. Raise
`wake.min_rms` so quiet playback does not open a window at all.

## Input injection

**Clicks land on the wrong pixel, or nothing is clicked.**
`alpha doctor` reports the backend. On Wayland the only supported backend is
`uinput`. Two known traps, both already handled in code:

- *Events sent too early are dropped.* Mutter adds a hotplugged virtual device
  asynchronously; events sent in the first ~2.5 s after creation are silently
  discarded. Alpha waits for the device node to appear and settles before the
  first event (`READY_SETTLE_S`).
- *Acceleration.* The pointer is created as an **absolute** device with
  `INPUT_PROP_POINTER`, so libinput treats it as a tablet and applies no pointer
  acceleration — which is what makes coordinates pixel-exact. If you swap the
  backend, verify this property or your clicks will drift at screen edges.

**`Permission denied: /dev/uinput`** → `sudo usermod -aG input "$USER"`, then
log out and back in.

**I need it to stop, now.**
`Ctrl+Alt+Q` (or `alpha abort`). The abort flag is checked between every loop
step and by the input backend, and it is created before any input device exists
so it can always fire. If you are using the free-tier provider, note that an
abort cannot cancel an HTTP request that is already in flight; it takes effect
at the next step.

## It gets OOM-killed, or crashed with no message

```bash
alpha doctor | grep -i memory        # compares running RSS against MemoryMax
alpha install-service                # re-render the unit after changing the config
```

Warm usage measured on this machine is **1176 MiB** (1003 daemon + 173 HUD) in
one cgroup, so a `MemoryMax` of 1024M will kill it mid-request. The default is
now 2048M. `alpha doctor` reports drift between `config.toml` and the deployed
unit, which is the usual cause of a stale limit.

## Speech is too slow, or the wrong language

- `stt.model = "small"` is chosen automatically above 8 GB RAM; `tiny.en` below.
  On a weak CPU, force `tiny.en` — it is several times faster and noticeably
  dumber.
- Arabic replies are spoken with the Arabic voice when the transcription is
  Arabic, else the English voice. Force it with `tts.voice`.
- `alpha warm` pre-loads whisper and piper so the first request is not slow;
  `alpha unload` frees them (and really frees them — it calls
  `malloc_trim(0)`, measured 1003 MiB → 292 MiB).

## Tests

```bash
uv run pytest -q          # 60 tests, no network, no mic, no input injection
```

If a test fails on `test_vision.py` or `test_unit.py`, the message names the
invariant (coordinate scaling and unit rendering respectively). Tests never
touch `~/.local/state/alpha/actions.jsonl` or the live daemon.

## Reporting a bug usefully

```bash
alpha doctor > /tmp/alpha-doctor.txt
journalctl --user -u alpha --since today --no-pager > /tmp/alpha-journal.txt
cp ~/.local/state/alpha/actions.jsonl /tmp/alpha-actions.jsonl
```

Check the journal for anything that looks like a key before attaching it — the
logger redacts secrets (`sk-…`) automatically, but attach at your own discretion.
