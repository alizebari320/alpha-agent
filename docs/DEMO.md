# Demo

Two honest questions to answer before anything else:

1. **What can you show someone in 3 minutes?**
2. **What have the tests already proven, so the demo does not have to?**

## The 3-minute demo (no microphone needed)

Microphones fail in demos. `alpha ask` runs the *identical* plan → act → verify
loop, with the transcript typed instead of spoken. The only difference is that
`alpha ask` skips the wake word and the VAD recorder.

```bash
# 0. show it is actually running, and on which provider
alpha doctor | head -30
alpha state

# 1. talk to it — pure LLM, no desktop action
alpha ask --no-speak "what is 2 plus 2"
#    -> 4

# 2. let it touch the desktop
alpha ask --no-speak "open Firefox"          # watch the audit log fill in:
tail -f ~/.local/state/alpha/actions.jsonl

# 3. show the audit trail is written BEFORE execution (safety claim)
#    the `bash firefox` line appears even if you abort

# 4. abort mid-flight
alpha abort                                   # or Ctrl+Alt+Q
alpha state
```

What step 2 proves, concretely, in the audit log:

```json
{"ts": ..., "tool": "bash", "command": "pgrep -x firefox"}
{"ts": ..., "tool": "bash", "command": "firefox"}
```

That is a real `Popen` of `firefox` — Firefox really comes up. (This is the path
that was broken by the `UnboundLocalError` described in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-agent-does-nothing-or-says-it-failed-but-the-audit-log-shows-an-action);
if the log shows the action but nothing happens, you are looking at that class
of bug — check `journalctl --user -u alpha`.)

## The voice demo, if you have a microphone

```bash
alpha state            # idle, muted=False
# say: "Hey Alpha"
#   -> chime, HUD turns to LISTENING
# say: "what is the capital of Iraq?"
#   -> HUD ACTING, then SPEAKING: "Baghdad."
```

To show the safety model:

```bash
# a destructive command must be confirmed out loud
alpha ask "delete my downloads folder"
#   -> spoken: "Should I run the command rm -rf ~/Downloads?"  (waits for "no")

# mute genuinely releases the microphone, it does not ignore samples
alpha mute && alpha state
```

## Recording it

Use the **text-mode** demo for a recorded demo. Reasons, not excuses: voice
demos need a quiet room, the wake word is a phrase not a speaker, and the free
LLM tier takes tens of seconds per step which looks broken on video even though
it is not. If you want the voice path on video, split the recording:

```bash
# One shell: the daemon's own log, as the visual proof of what it heard and did
journalctl --user -u alpha -f

# Another: OBS with the HUD in frame
alpha state
```

Scenes worth capturing, in order: `alpha doctor` (it is real and it is honest),
`alpha ask "what is 2 plus 2"` (the brain), `alpha ask "open Firefox"` with the
audit log beside it (the hands), `alpha abort` (the brakes), and
`tail -f actions.jsonl` (the accountability).

## What the tests already prove — do not re-demo it

| Claim | Proven by |
|---|---|
| Coordinate math (model ↔ screen ↔ monitor) is exact | `tests/test_vision.py` |
| The audit entry is written before execution | `tests/test_misc.py` |
| `ls; rm -rf ~` and `echo x > ~/.bashrc` are refused; `pgrep -a firefox \| head -5` is allowed | `tests/test_misc.py` |
| GUI launches do not block the loop for 30 s | `tests/test_misc.py` |
| The `bash` tool really executes | `tests/test_agent_loop.py::test_bash_tool_runs` |
| A local `import` can never shadow a module-level name in the executor again | `tests/test_agent_loop.py::test_wait_tool_does_not_bind_asyncio_locally` |
| A spoken/typed abort kills the loop within one step | `tests/test_agent_loop.py::test_abort_kills_loop` |
| The wake matcher normalises `ph → f` and fuzzy-matches per word | `tests/test_misc.py` |
| The systemd unit mirrors `memory_max_mb` (the 1024 vs 2048 bug) | `tests/test_unit.py` |
| A password field blocks input | `tests/test_vision.py` |

```bash
uv run pytest -q          # 60 tests, no network, no mic, no input injection
```

## The demo that is worth the most

Show it **refusing** something. `alpha ask "delete all my files"`, then the
spoken confirmation, then answer "no" and show the audit log recording the
refusal and the task stopping. Every agent can claim to click a button; the
interesting engineering is the guard rail, and Alpha's guard rail is the part
with the most tests behind it.
