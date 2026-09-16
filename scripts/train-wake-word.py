#!/usr/bin/env python3
"""Train a custom openWakeWord model for Alpha.

Alpha's default wake engine (`wake_word.mode = "kws"`) needs no training. Train a
model when you want a custom phrase *and* the low idle memory of openWakeWord
(see ../docs/WAKE_WORD.md for the trade-off table).

This script is a thin, honest driver around openWakeWord's OWN training pipeline
(`openwakeword/train.py` + a YAML config), the same one used by the official
Colab notebook. It does not reimplement any ML.

    scripts/train-wake-word.py --check                 # what is missing, exactly
    scripts/train-wake-word.py --print-config          # show the generated YAML
    scripts/train-wake-word.py --run                   # generate -> augment -> train
    scripts/train-wake-word.py --install MODEL.onnx     # deploy a Colab-trained model

Reality check, up front: full-quality training wants a GPU and ~30 GB of disk
for the negative-feature and background-audio corpora. `--check` reports the
real numbers for this machine before anything is downloaded. On a laptop the
practical path is the Colab notebook (notebooks/train_wake_word_colab.ipynb)
followed by `--install`.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Official openWakeWord training inputs (verified against the upstream notebook
# automatic_model_training.ipynb and openwakeword/train.py's own CLI flags).
HF = "https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main"
ASSETS = {
    "piper_repo": "https://github.com/rhasspy/piper-sample-generator",
    "piper_voice": (
        "https://github.com/rhasspy/piper-sample-generator/releases/download/"
        "v2.0.0/en_US-libritts_r-medium.pt"
    ),
    "train_features": f"{HF}/openwakeword_features_ACAV100M_2000_hrs_16bit.npy",
    "validation_features": f"{HF}/validation_set_features.npy",
    "audioset_part": "https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/bal_train09.tar",
}
# Approximate download sizes (bytes) so --check can be honest about disk usage.
SIZES = {
    "train_features": 63 * 1024**3 // 10,   # ~6.3 GB (int16, 2000 h)
    "validation_features": 36 * 1024**2,    # ~36 MB (11 h)
    "audioset_part": 5 * 1024**3,           # ~5 GB tar
    "piper_voice": 100 * 1024**2,
}
PY_DEPS = ["torch", "torchaudio", "yaml", "scipy", "numpy", "tqdm", "datasets",
           "pronouncing", "audiomentations", "webrtcvad", "mutagen"]


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n} B"


def check(workdir: Path) -> tuple[bool, list[str]]:
    """Report exactly what is installed, what is missing, and what it will cost."""
    import importlib.util

    print("=" * 72)
    print("Alpha wake-word trainer — environment check")
    print("=" * 72)

    missing_py: list[str] = []
    for dep in PY_DEPS:
        mod = {"yaml": "yaml", "webrtcvad": "webrtcvad"}.get(dep, dep.split(".")[0])
        ok = importlib.util.find_spec(mod) is not None if mod else False
        print(f"  {'✓' if ok else '✗'} python: {dep}")
        if not ok:
            missing_py.append(dep)

    # openwakeword training modules must be importable (they are in the venv)
    ow_ok = importlib.util.find_spec("openwakeword") is not None
    ow_train = REPO_ROOT / ".venv/lib/python3.11/site-packages/openwakeword/train.py"
    print(f"  {'✓' if ow_ok else '✗'} openwakeword installed")
    print(f"  {'✓' if ow_train.exists() else '✗'} openwakeword/train.py "
          f"(official pipeline driver)")

    piper = workdir / "piper-sample-generator"
    print(f"  {'✓' if (piper / 'generate_samples.py').exists() else '✗'} "
          f"piper-sample-generator checked out at {piper}")

    # GPU
    gpu = None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            gpu = out.stdout.strip().splitlines()[0]
    except Exception:
        pass
    print(f"  {'✓' if gpu else '·'} GPU: {gpu or 'none detected (CPU training is very slow)'}")

    # Disk
    need = sum(SIZES.values())
    free = shutil.disk_usage(workdir if workdir.exists() else Path.home()).free
    print(f"\n  disk: {human(free)} free at {workdir}; full pipeline needs "
          f"~{human(need)} of downloads plus ~{human(need // 2)} working set")
    if free < need:
        print("        ⚠ not enough space for the full negative corpora — use the "
              "Colab notebook instead, or lower n_samples and use --no-full-data")

    print("\n  blocking issues:")
    if missing_py:
        print(f"    • missing python packages: {' '.join(missing_py)}")
        print("      install with:")
        print(f"        uv pip install {' '.join(missing_py)}")
        if "torch" in missing_py:
            print("      (for CUDA: uv pip install torch --index-url "
                  "https://download.pytorch.org/whl/cu124)")
    if not ow_ok:
        print("    • openwakeword is not importable — run: uv pip install -e .")
    if not (piper / "generate_samples.py").exists():
        print(f"    • piper-sample-generator not checked out:\n"
              f"        git clone {ASSETS['piper_repo']} {piper}\n"
              f"        curl -L -o {piper}/models/en_US-libritts_r-medium.pt "
              f"{ASSETS['piper_voice']}")
    if not missing_py and ow_ok and (piper / "generate_samples.py").exists():
        print("    none — ready to train locally")

    ready = not missing_py and ow_ok and (piper / "generate_samples.py").exists()
    if not ready:
        print("\n  The Colab notebook runs the identical pipeline on a free GPU:")
        print("    notebooks/train_wake_word_colab.ipynb")
        print("  Then bring the .onnx back with:")
        print("    scripts/train-wake-word.py --install hey_alpha.onnx")
    return ready, missing_py


def build_config(phrase: str, workdir: Path, n_samples: int, n_samples_val: int,
                 steps: int, full_data: bool) -> Path:
    """Write the training YAML that openwakeword/train.py consumes."""
    import yaml

    name = phrase.strip().lower().replace(" ", "_")
    cfg = {
        "model_name": name,
        "target_phrase": [phrase],
        "custom_negative_phrases": [],
        "n_samples": n_samples,
        "n_samples_val": n_samples_val,
        "tts_batch_size": 50,
        "augmentation_batch_size": 16,
        "piper_sample_generator_path": str(workdir / "piper-sample-generator"),
        "output_dir": str(workdir / "output" / name),
        "rir_paths": [str(workdir / "mit_rirs")],
        "background_paths": [str(workdir / "audioset_16k"), str(workdir / "fma")],
        "background_paths_duplication_rate": [1],
        # These five keys are read by openwakeword/train.py and are part of the
        # official examples/custom_model.yml; omitting them makes the pipeline
        # fail with a KeyError, so they are set here rather than left to chance.
        "augmentation_rounds": 1,
        "total_length": 32000,
        "model_type": "dnn",
        "target_false_positives_per_hour": 0.2,
        "false_positive_validation_data_path": str(workdir / "validation_set_features.npy"),
        "feature_data_files": {
            "ACAV100M_sample": str(workdir / "openwakeword_features_ACAV100M_2000_hrs_16bit.npy")
        },
        "batch_n_per_class": {"ACAV100M_sample": 1024, "adversarial_negative": 50,
                              "positive": 50},
        "steps": steps,
        "max_negative_weight": 1500,
        "target_accuracy": 0.6,
        "target_recall": 0.25,
        "layer_size": 32,
    }
    if not full_data:
        cfg["feature_data_files"] = {}
        cfg["false_positive_validation_data_path"] = ""
    path = workdir / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path


def run_pipeline(cfg_path: Path, workdir: Path, stages: list[str]) -> int:
    """Run the official openwakeword/train.py stages, in order."""
    train_py = REPO_ROOT / ".venv/lib/python3.11/site-packages/openwakeword/train.py"
    if not train_py.exists():
        print(f"error: {train_py} not found — openwakeword is not installed.",
              file=sys.stderr)
        return 2
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{workdir}:{REPO_ROOT}"
    for stage in stages:
        cmd = [sys.executable, str(train_py), "--training_config", str(cfg_path),
               f"--{stage}"]
        print(f"\n$ {' '.join(cmd)}   (cwd={workdir})", flush=True)
        rc = subprocess.run(cmd, cwd=str(workdir), env=env).returncode
        if rc != 0:
            print(f"\n✗ stage --{stage} failed (exit {rc}).", file=sys.stderr)
            print("  The official pipeline is unchanged by Alpha, so the failure is "
                  "upstream:", file=sys.stderr)
            print("  re-run this exact command to see its full output. If it needs a "
                  "GPU you don't have,", file=sys.stderr)
            print("  use notebooks/train_wake_word_colab.ipynb and then --install.",
                  file=sys.stderr)
            return rc
        print(f"✓ stage --{stage} complete", flush=True)
    return 0


def install(model: Path, phrase: str, yes: bool) -> int:
    """Copy a trained model into Alpha's model dir and offer to wire it up."""
    from alpha import paths

    if not model.exists():
        print(f"error: {model} does not exist", file=sys.stderr)
        return 2
    name = phrase.strip().lower().replace(" ", "_")
    paths.ensure_dirs()
    dest = paths.DATA_DIR / "models" / f"{name}{model.suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(model, dest)
    print(f"installed {dest}  ({human(dest.stat().st_size)})")

    # A .tflite sibling is optional but cheaper at runtime; report honestly.
    sibling = model.with_suffix(".tflite")
    if sibling.exists() and sibling != dest:
        dest_tf = dest.with_suffix(".tflite")
        shutil.copy2(sibling, dest_tf)
        print(f"installed {dest_tf}  ({human(dest_tf.stat().st_size)})")

    cfg = paths.CONFIG_FILE
    print("\nEnable it with:")
    print("  [assistant.wake_word]")
    print('  mode = "pretrained"')
    print(f'  model = "{name}"')
    if yes and cfg.exists():
        text = cfg.read_text()
        text = text.replace('mode = "kws"', 'mode = "pretrained"')
        if f'model = "{name}"' not in text:
            text = text.replace('model = "hey_jarvis"', f'model = "{name}"')
        cfg.write_text(text)
        print(f"\n✓ patched {cfg}")
        print("  restart: systemctl --user restart alpha")
        print("  verify : journalctl --user -u alpha -f | grep 'wake word (pretrained)'")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Train a custom openWakeWord model for Alpha",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phrase", default="hey alpha", help="wake phrase to train")
    ap.add_argument("--workdir", default=str(Path.home() / ".local/share/alpha/wake-training"),
                    help="where clips and checkpoints live")
    ap.add_argument("--n-samples", type=int, default=2000, help="synthetic positives")
    ap.add_argument("--n-samples-val", type=int, default=1000, help="validation positives")
    ap.add_argument("--steps", type=int, default=10000, help="training steps")
    ap.add_argument("--check", action="store_true", help="report prerequisites and exit")
    ap.add_argument("--print-config", action="store_true", help="write/show the YAML only")
    ap.add_argument("--run", action="store_true", help="generate → augment → train")
    ap.add_argument("--stages", default="generate_clips,augment_clips,train_model",
                    help="comma-separated subset of the official stages")
    ap.add_argument("--no-full-data", action="store_true",
                    help="skip the multi-GB negative corpora (quick, lower quality)")
    ap.add_argument("--install", metavar="MODEL", nargs="?",
                    help="install a trained .onnx (e.g. from Colab) and stop")
    ap.add_argument("--yes", action="store_true", help="patch config.toml without asking")
    args = ap.parse_args()

    workdir = Path(args.workdir).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)

    if args.install:
        return install(Path(args.install).expanduser(), args.phrase, args.yes)

    ready, _ = check(workdir)

    if args.check:
        return 0 if ready else 1

    cfg_path = build_config(args.phrase, workdir, args.n_samples, args.n_samples_val,
                            args.steps, not args.no_full_data)
    print(f"\nwrote training config: {cfg_path}")
    if args.print_config:
        print(cfg_path.read_text())
        return 0

    if not args.run:
        print("\nnothing to do: pass --run to execute the pipeline, "
              "--print-config to inspect the YAML, or --install to deploy a model.")
        return 0

    if not ready:
        print("\nrefusing to start: prerequisites are missing (see the list above). "
              "Nothing was downloaded.", file=sys.stderr)
        return 1

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    rc = run_pipeline(cfg_path, workdir, stages)
    if rc != 0:
        return rc

    name = args.phrase.strip().lower().replace(" ", "_")
    out = workdir / "output" / name
    produced = sorted(out.glob("*.onnx")) + sorted(out.glob("*.tflite"))
    print(f"\nartifacts in {out}:")
    for f in produced:
        print(f"  {f.name}  ({human(f.stat().st_size)})")
    if produced:
        print("\nNext: scripts/train-wake-word.py "
              f"--install {produced[0]} --phrase {args.phrase!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
