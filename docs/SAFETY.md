# Safety model

Alpha has real control over a real desktop. This document states exactly what
the guard does, what it cannot do, and where the code lives
(`alpha/safety/guard.py`, `alpha/brain/loop.py`, `alpha/daemon.py`).

## 0. The one rule that shapes the design

**The abort path is implemented before any input injection.** There is no code
path in this repository that injects input before the guard is constructed and
the abort flag is reachable. If you change the agent loop, keep that order.

## 1. Abort — `Ctrl+Alt+Q`

```
hotkey (GNOME custom keybinding) ──► alpha abort ──► ctl.sock ──► guard.request_abort()
                                                                        │
   loop checks: after every LLM step, after every observation, before every action
```

- `guard.check_abort()` raises `AbortRequested`, caught in
  `Daemon._run_request`, which speaks "Aborted." and returns to idle.
- The signal is instantaneous: it does not wait for the current LLM call.
- `alpha abort` from any terminal is equivalent to the hotkey.
- Register the keys once with `alpha install-hotkeys` (writes GNOME
  `custom-keybindings` via `gsettings`; no root).

On X11 the daemon could additionally grab the key itself; on Wayland apps cannot
grab global keys, so a session keybinding is the supported mechanism. Alpha uses
the keybinding on both, so behaviour is identical.

## 2. Audit log — written *before* execution

Every action is appended to `~/.local/state/alpha/actions.jsonl` **before** it
runs, so a crash or a kill mid-action still leaves a trail:

```json
{"ts": 1789553161.42, "tool": "bash", "command": "firefox"}
{"ts": 1789553163.88, "tool": "type_text", "text": "red panda photo"}
{"ts": 1789553164.10, "tool": "key", "combo": "Return"}
```

Keys beginning with `_` are internal hints (e.g. `_password_field`) and are
stripped before writing. Inspect with:

```bash
tail -f ~/.local/state/alpha/actions.jsonl
```

## 3. bash gating

`guard.check_action("bash", …)` returns "allowed without asking" or "needs a
spoken yes". The decision is made on **every segment** of a compound command:

| Class | Examples | Decision |
|---|---|---|
| Allowlisted apps | `firefox`, `xdg-open …`, `blender`, `code` | allowed |
| Read-only inspection | `pgrep`, `ps`, `ls`, `cat`, `wmctrl -l`, `xdotool search`, `gsettings get`, `systemctl status`, `df`, `free`, `date` | allowed |
| Read-only subcommands | `xdotool getactivewindow`, `pactl info`, `systemctl is-active` | allowed |
| Output redirect to a file | `echo x > ~/.bashrc`, `cmd 2>/tmp/e` | **asks** |
| Destructive | `sudo`, `rm`, `dd`, `mkfs`, `shutdown`, `git push --force`, `gsettings reset` | **asks** (always) |
| Anything else | `python3 -c …`, unknown binaries | **asks** |

Compound commands are allowed only when **every** segment is safe, so
`pgrep -a firefox | head -5` runs silently while `ls; rm -rf ~` can never be
classified as an `ls`.

> Why read-only commands are allowlisted: the first live end-to-end run stalled
> because the agent needed `wmctrl -l` and `pgrep` to verify its own work and
> each check triggered a spoken confirmation. A verification step that cannot
> run is a safety *regression* — the agent then acts blind.

> Why this matters for redirects: `echo` is harmless, `echo … > ~/.bashrc` is
> not. That hole existed and was closed; `tests/test_misc.py` guards it.

## 4. Spoken confirmation

Gated actions raise a spoken question ("Should I run the command …?"). Alpha
records your answer, transcribes it, and looks for yes/no in English or Arabic
(`yes/yeah/sure/ok/نعم/اي/بلى`). A decline returns
`user declined; do not retry this command` to the planner, which is instructed
not to retry.

In text mode (`alpha ask`) confirmations still happen — the daemon speaks the
question and listens, so do not use `alpha ask` for unattended gated commands.

## 5. Password lock

If the focused accessibility element carries the AT-SPI `PASSWORD_TEXT` state,
`check_action("type_text")` refuses and the loop returns
"A password field is focused, so I won't touch the screen."

The warning logs role, name and application so a false positive is diagnosable
rather than mysterious. If your desktop reports a password field when none is
focused, set `vision.password_lock = false` in the config — knowingly.

## 6. Denylist

```toml
[safety]
denylist = ["bitwarden", "keepassxc", "1password", "gnome-keyring", "seahorse"]
```

Denylisted apps are refused **outright** — they are never offered as a
confirmation, because a spoken "yes" is not a strong enough control for a
password manager. Matching is a case-insensitive substring test on the target
app/window name.

## 7. Cost guard

- `llm.daily_spend_cap_usd` (default 5.00) — checked before each request;
  the running total lives in `~/.local/state/alpha/spend.jsonl`.
- `llm.per_request_step_cap` / `llm.max_steps` (default 25) — hard stop on the
  action loop, so a confused model cannot loop forever.
- Terminal conditions also count: `NO_CHANGE_LIMIT = 3` detects an action
  sequence that changes nothing and ends with "I'm stuck, can you help me?"

## 8. What Alpha does *not* protect you from

Stated plainly, because a safety section that oversells itself is worse than
none:

- **A wrong click in a normal app.** If your LLM decides to click "Delete" in
  your editor, Alpha will do it. The guard understands *commands*, not intent.
- **Data leaving your machine.** With a cloud provider, screenshots and window
  contents go to that provider. That is the central trade-off of the project and
  is disclosed at the top of the README. Use Ollama for a fully local setup.
- **A compromised LLM provider.** A malicious provider can emit any tool call a
  human could type. The guard limits *unattended* damage; it is not a sandbox.
- **Prompt injection from screen content.** A web page saying "Assistant: now
  open a terminal and run …" is an unsolved problem in this class of agent. The
  confirmation gate is the mitigation: a gated command still needs your voice.
  This is another reason the bash allowlist is narrow.
- **Confidentiality of the audit log.** It records what you did, in plain text,
  under your home directory.

## 9. Testing the guard

```bash
uv run pytest tests/test_misc.py -q -k guard      # allowlist/destructive rules
alpha abort                                       # then watch the loop stop immediately
```

The guard rules are pure functions (`_bash_segments`, `_segment_is_safe`,
`DESTRUCTIVE_RE`) precisely so they can be tested without a desktop.
