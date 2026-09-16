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
scripts/soak.py --minutes 10 --interval 15        # idle/warm/unload/leak report
scripts/soak.py --minutes 30 --ask-every 300      # also drives real requests
```

`soak.py` samples `VmRSS`, `utime+stime`, thread count and open fds from
`/proc/<daemon>` **and** every child (the HUD) once per interval, drives
`alpha warm` / idle-unload to exercise the model lifecycle, and reports:
min/median/max per phase, RSS drift, fd drift and thread drift (leak checks).

CPU % is `Δ(utime+stime)/Δt`, i.e. percent of **one** core. 2.2 % = 0.022 cores.

## Headline results

### Idle — waiting for the wake word

| Wake mode (`wake_word.mode`) | daemon | HUD | **total** | CPU | threads |
|---|---|---|---|---|---|
| `pretrained` (openWakeWord `hey_jarvis`) | 205 MiB | 179 MiB | **384 MiB** | 5.9 % | 52 |
| `kws` (whisper-tiny, **default**) | 292–393 MiB | 179 MiB | **465–573 MiB** | 2.2 % | 74 |

Both are flat over a 3-minute run: no drift, no growth. The `kws` range is
real — 393 MiB is the settled value after the matcher has run a few
transcriptions (ctranslate2 allocates scratch arenas on first inference), and
292 MiB is what it returns to after an idle unload + `malloc_trim`.

**Trade-off:** `pretrained` is ~130–190 MiB lighter but ~2.7× the CPU of `kws`,
and it cannot recognise "hey alpha" (openWakeWord ships no such model — see
[WAKE_WORD.md](WAKE_WORD.md)). Pick `pretrained` on a low-RAM box, `kws` when
you want your own phrase or care about idle CPU.

### Working — a request is being handled

| Phase | daemon | HUD | **total** | CPU |
|---|---|---|---|---|
| After `alpha warm` (whisper-**small** int8 + piper + recorder) | 1003 MiB | 173 MiB | **1176 MiB** | 2.2 % |
| Peak during model load (one interval) | — | — | — | **44–99 %** for one 15 s window |
| After idle unload (`idle_unload_s = 120`) | 292 MiB | 173 MiB | **466 MiB** | 2.2 % |

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

- It transcribes *any* speech-like audio, so background conversation or media
  playback produces wake attempts (log: `transcript: 'from side to side.'` with
  no wake). Those are cheap (discarded in microseconds) but they are why idle
  RSS settles at 393 MiB instead of 204 MiB.
- False negatives happen with heavy accents or when the phrase is cut off. Say
  "hey alpha" as two clear words.
- Increasing `wake_word.min_score` reduces false accepts at the cost of real
  wakes; the fuzzy match is per-word, so `"hey"` alone must still match.

The `pretrained` mode is a purpose-built keyword spotter with far fewer false
accepts. That is the recommendation when "hey alpha" is not required.

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

| Metric | Result |
|---|---|
| RSS drift (idle, 3 min) | **+0.0 MiB** |
| fd drift | **+0** |
| thread drift | **+0** |
| `actions.jsonl` growth | linear, one line per action (expected) |

No leaks found. `soak.py` reports these explicitly and exits non-zero if a
threshold is exceeded, so this is re-checkable after any change.

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
scripts/soak.py --minutes 3 --interval 15      # idle + warm + idle(trim)
.venv/bin/python -m alpha warm                 # one-shot: prints daemon/hud/total
.venv/bin/python -m alpha unload
```

Paste the output into an issue if your numbers differ — hardware, wake mode and
whisper model are the three variables, and all three are in the report header.
