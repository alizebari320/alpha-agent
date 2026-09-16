# Wake word — renaming it and training your own

Alpha ships two wake engines. Which one you get is one config line:

```toml
[assistant.wake_word]
mode = "kws"          # "kws" (default) or "pretrained"
phrase = "hey alpha"  # kws mode: any phrase you like
model = "hey_jarvis"  # pretrained mode: an openWakeWord model name
min_score = 0.6
```

| | `kws` (default) | `pretrained` |
|---|---|---|
| Engine | whisper-tiny transcribing VAD-gated 2 s windows, then a per-word fuzzy match | openWakeWord's purpose-built keyword spotter (ONNX/tflite) |
| Custom phrase | **yes, anything** | only the shipped models |
| Idle RAM (daemon) | 292–393 MiB | 205 MiB |
| Idle CPU | ~2.2 % | ~5.9 % |
| False accepts | more (it transcribes any speech and matches) | few |

The reason `kws` is the default: **openWakeWord ships no `hey_alpha` model.**
Its pretrained set is `hey_jarvis`, `hey_mycroft`, `hey_rhasspy`, `alexa`. If
you want "hey Alpha" out of the box, you need `kws` — or you train your own
model (below) and switch to `pretrained` with it.

## Renaming the wake word (no training)

`kws` mode takes any phrase, so this is a config edit and a restart:

```toml
[assistant]
wake_word = { mode = "kws", phrase = "hey computer", min_score = 0.6 }
```

or:

```bash
alpha init            # interactive: also sets the assistant name
systemctl --user restart alpha
journalctl --user -u alpha | grep "wake word mode"
```

Tips, from testing:

- **2–4 syllables ending in a stressed vowel** work best ("hey alpha", "hey
  jarvis", "ok computer"). One-syllable phrases false-trigger constantly.
- The matcher normalises phonetics (`ph` → `f`), so "hey alfa" also matches.
- `min_score` (0–1) is the per-word fuzzy threshold. Raise it to 0.7+ if you get
  false wakes; lower to 0.5 if you have to shout. The matcher requires **all**
  words of the phrase to match, not just one.
- Whisper-tiny mangles unfamiliar names. "hey alva"/"hay alfa" still pass thanks
  to fuzzy matching; if your phrase fails, check the log — every rejected
  transcription is printed, so you can see what it actually heard:

  ```bash
  journalctl --user -u alpha -f | grep transcript
  ```

## Training a real wake-word model (the recommended path)

Because openWakeWord has no `hey_alpha`, the durable fix is to train one. The
project ships a script that drives openWakeWord's **official** training pipeline
(synthetic piper TTS positives + ACAV100M negatives + augmentation):

```bash
scripts/train-wake-word.py --phrase "hey alpha" --out ~/.local/share/alpha/models/hey_alpha.onnx
```

and a Colab notebook for a free GPU (the full pipeline wants one):

```
notebooks/train_wake_word_colab.ipynb
```

Both run the same upstream steps, so a model trained in Colab drops straight in:

```toml
[assistant.wake_word]
mode = "pretrained"
model = "hey_alpha"                       # resolves to <models_dir>/hey_alpha.onnx
min_score = 0.5
```

Then restart and watch it fire:

```bash
systemctl --user restart alpha
journalctl --user -u alpha -f | grep "wake word (pretrained)"
# → INFO alpha.wake: wake word (pretrained): hey_alpha
```

### What the training pipeline actually does

1. **Generate positives** — ~1000–3000 synthetic clips of the phrase spoken by
   many piper voices, at varying speeds and pitches, mixed with noise and music
   beds from the augmentation corpora.
2. **Negative features** — precompute melspectrogram features for a large
   negative set (ACAV100M subset + the "false positive" words that sound close:
   *hey alfalfa*, *hey alpha centauri*, *they offer*…). This is what stops the
   model from firing on conversation.
3. **Train** — a small convolutional head over openWakeWord's shared
   melspectrogram + embedding models, ~10–30 minutes on a Colab T4.
4. **Validate** — report the *false-accept rate per hour* on held-out negatives
   and the recall on held-out positives. Alpha's log prints both; anything above
   ~0.5 FA/hour will annoy you.

Training config lives in `scripts/train-wake-word.py --help`; the Colab notebook
exposes the same knobs as form fields.

### Model file layout

```
~/.local/share/alpha/models/hey_alpha.onnx
~/.local/share/alpha/models/hey_alpha.tflite     # optional, lower CPU
```

`ensure_openwakeword()` looks for the file first and only falls back to
openWakeWord's bundled models, so a local `.onnx` always wins. `alpha models`
prints which one resolved.

## Which mode should you pick?

- **Just want it to work, on a machine with ≥4 GB free:** `kws`, phrase
  "hey alpha". Nothing to train.
- **Low RAM, don't care about the phrase:** `pretrained` with `hey_jarvis`
  (205 MiB idle, no whisper at all while idle).
- **Low RAM *and* a custom phrase:** train the model above, then `pretrained`.
  This is the best configuration if you are willing to spend 30 minutes in
  Colab.

## Tuning the detector

```toml
[assistant.wake_word]
sensitivity = 0.5      # kws: VAD energy gate; pretrained: openWakeWord threshold
debounce_s = 2.0       # ignore a second wake within this window
```

- Wake while Alpha is *speaking* is ignored (no self-triggering from the mic
  picking up its own TTS) — the daemon checks its own state first.
- `Ctrl+Alt+M` mutes the mic entirely (the stream is closed, the LED goes out).
  A muted Alpha never wakes.
- If the wake word fires from media playback, use headphones or `pretrained`
  mode; the `kws` matcher cannot distinguish your voice from a podcast, and that
  is a property of the approach, not a bug we can tune away.
