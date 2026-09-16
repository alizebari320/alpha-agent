# Performance & resource use

Every number here was measured on the machine Alpha was developed on, with
`scripts/soak.py`, and is reproduced verbatim. Where a number is unflattering,
it is still written down — this document exists so you can budget RAM *before*
you install, not after.

## Test machine

| | |
|---|---|
| OS | Fedora 44, GNOME 49 on **Wayland** (Mutter) |
| CPU | 16 threads |
| RAM | 16 GB |
| GPU | NVIDIA (proprietary driver) + Mesa/LLVM |
| Python | 3.11.13 in `.venv`, 3.14 system |
| Display | single `eDP-1` 1920×1080 @ scale 1 |

## Method

```bash
scripts/soak.py --minutes 10 --interval 15        # idle/warm/cooled/leak report
scripts/soak.py --minutes 30 --ask-every 300      # also drives real requests
```

`soak.py` samples `VmRSS`, `utime+stime`, thread count and open fds from
`/proc/<daemon>` **and** every child (the HUD) once per interval, drives
`alpha warm` / idle-unload to exercise the model lifecycle, and reports:
min/median/max per phase, RSS drift, fd drift and thread drift (leak checks).

CPU % is `Δ(utime+stime)/Δt`, i.e. percent of **one** core. 2.2 % = 0.022 cores.

Phases are driven by the harness, not left to the daemon's own idle timer:
`idle → warm → cooled → idle`. Earlier versions only ever entered `warm` and then
averaged three different memory states (731 MiB warm, 215 MiB unloaded, 377 MiB
after the wake path's first decode) under one label, which made the report
quietly wrong.

## Headline results

### Idle — waiting for the wake word

| Wake mode (`wake_word.mode`) | daemon | HUD | **total** | CPU | threads |
|---|---|---|---|---|---|
| `pretrained` (openWakeWord `hey_jarvis`) | 205 MiB | 179 MiB | **384 MiB** | 5.9 % | 52 |
| `kws`, quiet room, nothing decoded yet | 172 MiB | 179 MiB | **351 MiB** | 1.2 % | 52 |
| `kws`, settled (adaptive gate, **current** code) | 194–217 MiB | 179 MiB | **373–396 MiB** | **1.7 % avg / 2.4 % max** | 53–57 |
| `kws`, before the adaptive gate, same room | 342–356 MiB | 179 MiB | **521–546 MiB** | 11.6 % avg / 18 % max | 55–59 |

The spread is not noise, it is four different states, and they are worth
knowing before you budget:

- **172 MiB** — the wake matcher object exists but whisper-tiny has not been
  asked to transcribe anything yet (the model loads lazily).
- **342 MiB** — whisper-tiny is resident because the wake path has transcribed
  at least one window. ctranslate2 allocates its scratch arenas on first
  inference and keeps them.
- **343 MiB / 5.7 % CPU** — same memory, but the room was talking, so the gate
  let windows through and the decoder ran.

Flat over a 6-minute soak otherwise: +0 fds, and thread count returns to its
starting value after the models unload.

The last two rows are the same machine, same room, same 6-minute soak, before and
after the adaptive wake gate — and the memory difference is downstream of the CPU
difference: when the gate stops letting room noise through, whisper-tiny is never
asked to transcribe anything, so it never loads at all (172 MiB) instead of
loading and settling at 342 MiB. Idle CPU fell from 11.6 % to **1.7 %** of a core.

| Phase | Before the adaptive gate | After |
|---|---|---|
| idle, daemon RSS | 342–356 MiB | **194–217 MiB** |
| idle, CPU | 11.6 % avg, 18.0 % max | **1.7 % avg, 2.4 % max** |
| after `alpha unload` | 344 MiB, 5.7 % | **217 MiB, 2.3 %** |
| `alpha warm` | 888 MiB, 28.5 % | **597 MiB, 10.5 %** |
| wake-path decodes in 6 min | 31 (all hallucinated "Thank you.") | **1** (real speech, `'Yeah'`) |

The drift figure in that run is +46 MiB, and it is *not* a leak: it is the one
real gate opening loading whisper-tiny (342-172 = 170 MiB is impossible to hide,
and the same run's `cooled` phase is flat at 216.6 MiB across three samples
180 s apart). The leak check only means what it says when nothing loads mid-run,
which is why `soak.py` reports the phases separately.

**Trade-off:** `pretrained` is ~130–190 MiB lighter but ~2.7× the CPU of `kws`,
and it cannot recognise "hey alpha" (openWakeWord ships no such model — see
[WAKE_WORD.md](WAKE_WORD.md)). Pick `pretrained` on a low-RAM box, `kws` when
you want your own phrase or care about idle CPU.

### Working — a request is being handled

| Phase | daemon | HUD | **total** | CPU |
|---|---|---|---|---|
| After `alpha warm`, `kws` matcher never decoded | 1003 MiB | 173 MiB | **1176 MiB** | 2.2 % |
| After `alpha warm`, `kws` matcher resident too | 982–1003 MiB | 179 MiB | **1161–1182 MiB** | 2.2 % |
| Peak during model load (first interval) | — | — | — | **44–99 %** for one 15 s window |
| After `alpha unload` (`malloc_trim`), matcher already resident | 344 MiB | 179 MiB | **523 MiB** | 2.2 % |
| After `alpha unload`, fresh daemon that never decoded | 215 MiB | 179 MiB | **394 MiB** | 2.2 % |

`alpha unload` frees the *request* models (whisper-small, piper) but deliberately
keeps the wake matcher — that is the spec's "only the wake model lives at idle".
In `kws` mode the wake matcher **is** whisper-tiny, which is why the post-unload
floor is 215 MiB (never decoded) or 344 MiB (tiny already resident) rather than
zero. Only `pretrained` mode gets the matcher down to an ONNX classifier.

The warm figure is the number that matters for the service limit, and it is why
`memory_max_mb` defaults to **2048**:

> **Measured bug:** with the original 1024 MB `MemoryMax`, a warm daemon
> (1003 MiB) plus the HUD (173 MiB) = 1176 MiB in one cgroup would have been
> **OOM-killed by systemd** mid-request. The default was raised to 2048 MB after
> this measurement. If you set it lower, use `stt.model = "base.en"` or
> `"tiny.en"` and expect `alpha warm` to still cost ~600 MiB.

### `malloc_trim`: why the idle unload actually works

Dropping the last Python reference to a whisper model does **not** reduce RSS:
glibc keeps the freed arenas and onnxruntime keeps its own pool. Measured
before the fix — `alpha unload` reported 612 MiB before *and* after; the spec's
promise ("only the wake model lives at idle") was false.

`_release_free_memory()` calls `libc.malloc_trim(0)` after unloading, which
hands those arenas back. Same measurement after the fix: **1003 MiB → 292 MiB**.
This is a 90-line-regression risk, so it is asserted in the soak output
(unload must reduce RSS) rather than trusted.

## Wake-word detection quality

`kws` mode is not a toy wake-word engine; it is whisper-tiny transcribing
VAD-gated 2-second windows and fuzzy-matching the phrase with phonetic
normalisation (`ph`→`f`, so "hey alfa" matches). Consequences, measured:

### Every decode costs ~0.65 s of CPU, no matter how short the window

This is the single most important number for `kws` mode, and it is
counter-intuitive:

| Audio decoded | CPU per decode (1 thread) |
|---|---|
| 2.0 s | **653 ms** |
| 1.0 s | 606 ms |
| 0.5 s | 567 ms |

Whisper does not scale down with input length here: faster-whisper pads every
call to a 30-second mel chunk, so most of the cost is fixed. Decoding a *shorter*
window buys nothing, and a window the gate lets through is charged in full even
when the transcript is discarded as "not the wake word".

So the wake gate **is** the CPU budget. It requires both a speech-like VAD ratio
(>= 0.15 of 80 ms frames) and a level above `assistant.wake_word.min_rms`
(default `300`, int16 RMS). In a quiet room almost nothing passes and idle sits
at ~1–2 % of one core. In a room with continuous speech — conversation, a
podcast, a TV — windows pass every couple of seconds, and the same daemon was
measured at **11.6 % average / 18 % peak** CPU over a 6-minute soak (with
`min_rms = 120`), and **up to 85 % single-sample spikes** in the 10-minute soak
run before the gate was tightened. That is the honest cost of recognising an
arbitrary phrase with a general-purpose speech model, and it is why:

- The background decoder is pinned to **one CPU thread** (`cpu_threads = 1`).
  With CTranslate2's default pool, the same 25-second workload cost **112.9 %**
  of one core; at one thread it cost **90.7 %**, and every other core stays free
  for your actual work. The *request* transcriber still uses the whole machine,
  because there latency matters and it runs once per request.
- `min_rms` is the *absolute* floor and is tunable: `alpha doctor --levels`
  samples your microphone and prints, per window, the level and VAD ratio next
  to the thresholds, plus whether that window would be decoded. On the
  development machine room tone measured **rms 280–955** with speech around
  **2000**, which is why the default is 300 rather than 120.
- On top of that floor the gate is **adaptive**. A fixed number cannot fit every
  room, and the failure mode is expensive: in one quiet 6-minute stretch the
  gate opened **31 times** on room noise, whisper answered every one with its
  classic hallucination set (`'Thank you.'`, `'I'm sorry.'`, `'You like your
  hair?'`), and the daemon burned **11.6 %** of a core to recognise nothing.
  `listen.NoiseFloor` keeps the 10th percentile of the last ~1 minute of window
  levels and requires a window to clear **2× room tone** as well as `min_rms`.
  Intermittent speech does not move a low percentile, so the floor tracks
  room tone; a genuinely silent room cannot normalise its way down, because the
  absolute floor always applies. Verified: noise-only windows (rms 400, VAD
  ratio 0.05) cost **0** decodes, a voice at rms 900 in that same room still
  gets through, and every gate opening is logged at INFO
  (`kws gate OPEN: ratio=0.62 rms=2140`) so this is never invisible again.

### Other measured behaviour

- Background speech produces wake *attempts* (log: `transcript: 'from side to
  side.'` with no wake) and is what moves idle RSS from 172 MiB to ~350 MiB.
- False negatives happen with heavy accents or when the phrase is cut off. Say
  "hey alpha" as two clear words.
- Raising `wake_word.threshold` (fuzzy-match score, default 0.6) reduces false
  accepts at the cost of real wakes; the fuzzy match is per-word, so a lone
  "hey" still matches.

The `pretrained` mode is a purpose-built keyword spotter: ONNX inference over
80 ms frames, no decoding at all, far fewer false accepts. **If you care about
background CPU, train "hey alpha" once** (`scripts/train-wake-word.py`) and run
`pretrained` with your own model — that is the path that makes the idle budget
effortless. `kws` exists so the assistant works with any name *before* you train
anything.

## Latency

End-to-end, from the moment you stop speaking:

| Stage | Measured |
|---|---|
| VAD end-of-speech detection | 0.6 s of trailing silence (configurable) |
| Whisper transcription (small, int8, 16 threads) | ~0.4–1.0 s for a short sentence |
| **LLM call (free tier used for testing)** | **10–60 s** ← dominates everything |
| piper TTS synthesis | ~0.2–0.5 s per sentence |
| Action step (uinput move+click) | <30 ms; `bash` up to 30 s |

The local pipeline is 1–2 s; the LLM provider is the entire latency budget.
This is a *config* problem, not an architecture problem:
`llm.planner.model = "…"` with a faster/paid model is the fix. Measured free-tier
providers also produced 429s and quota errors, which `_describe_error()` turns
into spoken explanations instead of stack traces.

## Leak checks

Over the soak runs reported above:

| Metric | 3-min run | 6-min run (with wake→warm→unload cycles) |
|---|---|---|
| RSS drift (idle → idle) | **+0.0 MiB** | +25 MiB, then flat |
| fd drift | **+0** | **+0** |
| thread drift | **+0** | +4 during the run, back to start after unload |
| `actions.jsonl` growth | linear, one line per action (expected) | same |

No leaks found in either run. The 6-minute +25 MiB / +4 threads both appear the
first time the wake matcher decodes, and both settle afterwards — they are
CTranslate2 allocating its arena and pool once, not unbounded growth. A single
30-minute run is still the right way to be sure, and `soak.py` exits non-zero if
a drift threshold is exceeded, so the claim is re-checkable after any change.

## Where the memory actually goes

`pmap` on the HUD shows the 179 MiB is **not** Alpha's code:

```
libnvidia-gpucomp.so   17.0 MB
libLLVM.so             14.6 MB
libgtk-4.so.1           ~9 MB
[anon] 37 MB           (GTK/Pango/font + icon caches)
```

It is a GTK4 process in a session with the NVIDIA proprietary driver and
Mesa/LLVM: the graphics stack is the cost, and it is paid once for the overlay,
tray icon and screenshots. There is no configuration that makes a GTK4
wayland-client cheap; the honest options are "accept ~180 MiB" or "don't run
the HUD".

On the daemon side, the warm 1003 MiB is almost entirely whisper-small int8
(~560 MiB resident during inference plus ctranslate2 arenas). `stt.model =
"tiny.en"` cuts that several-fold with a real accuracy cost for long requests.

## Reproducing

```bash
systemctl --user restart alpha && sleep 20
scripts/soak.py --minutes 6 --interval 30      # idle + warm + cooled + drift
.venv/bin/python -m alpha warm                 # one-shot: prints daemon/hud/total
.venv/bin/python -m alpha unload
.venv/bin/python -m alpha doctor --levels      # what the wake gate sees
```

Paste the output into an issue if your numbers differ — hardware, wake mode and
whisper model are the three variables, and all three are in the report header.
