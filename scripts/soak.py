#!/usr/bin/env python3
"""Soak test: measure Alpha's real memory, CPU and stability over time.

This exists because the numbers in docs/PERFORMANCE.md must come from a real
run, not from guesses. It samples the daemon and its children (the GTK HUD)
from /proc, tracks CPU time, threads and file descriptors, verifies the state
machine still answers, and prints a markdown report.

Usage
-----
    scripts/soak.py --minutes 30                 # idle soak
    scripts/soak.py --minutes 30 --ask-every 300 # also send a request every 5min
    scripts/soak.py --minutes 2 --interval 5     # quick smoke run

Notes
-----
* `--ask-every` spends provider quota. It is off by default.
* The wake->request cycle needs a microphone; `alpha warm` loads exactly the
  same models the voice path loads, so warm/unload phases are equivalent
  memory-wise and scriptable without a mic.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SERVICE = "alpha"


def _proc_stats(pid: int) -> dict:
    """rss MiB, cpu seconds, threads and open fds for one pid."""
    out = {"rss_mb": 0.0, "cpu_s": 0.0, "threads": 0, "fds": 0}
    try:
        with open(f"/proc/{pid}/statm") as f:
            pages = int(f.read().split()[1])
        out["rss_mb"] = pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return out
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(") ", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        out["cpu_s"] = (int(fields[11]) + int(fields[12])) / ticks
        out["threads"] = int(fields[17])
    except (OSError, ValueError, IndexError):
        pass
    try:
        out["fds"] = len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        pass
    return out


def _children(pid: int) -> list[int]:
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            return [int(p) for p in f.read().split()]
    except OSError:
        return []


def sample() -> dict:
    """One measurement of the whole service (daemon + children)."""
    try:
        main = int(subprocess.run(
            ["systemctl", "--user", "show", SERVICE, "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=10).stdout.strip() or 0)
    except Exception:
        main = 0
    if not main:
        return {"alive": False}
    me = _proc_stats(main)
    kids = {c: _proc_stats(c) for c in _children(main)}
    try:
        active = subprocess.run(["systemctl", "--user", "is-active", SERVICE],
                                capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        active = "unknown"
    return {
        "alive": True,
        "state": active,
        "daemon_mb": me["rss_mb"],
        "hud_mb": sum(k["rss_mb"] for k in kids.values()),
        "total_mb": me["rss_mb"] + sum(k["rss_mb"] for k in kids.values()),
        "cpu_s": me["cpu_s"] + sum(k["cpu_s"] for k in kids.values()),
        "threads": me["threads"] + sum(k["threads"] for k in kids.values()),
        "fds": me["fds"] + sum(k["fds"] for k in kids.values()),
    }


def ctl(cmd: str, **extra) -> dict:
    """Talk to the daemon over its control socket (no third-party imports)."""
    import socket

    sock_path = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "alpha/ctl.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(600 if cmd == "ask" else 60)
    try:
        s.connect(str(sock_path))
        s.sendall(json.dumps({"cmd": cmd, **extra}).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.split(b"\n")[0] or b"{}")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    finally:
        s.close()


def fmt_table(rows: list[dict]) -> str:
    head = ("| elapsed | phase | daemon MiB | HUD MiB | total MiB | CPU s | CPU % | "
            "threads | fds | state |\n|---|---|---|---|---|---|---|---|---|---|")
    lines = [head]
    for r in rows:
        lines.append(
            f"| {r['elapsed']:>7} | {r['phase']} | {r['daemon_mb']:.1f} | {r['hud_mb']:.1f} | "
            f"{r['total_mb']:.1f} | {r['cpu_s']:.2f} | {r.get('cpu_pct', 0):.2f} | "
            f"{r['threads']} | {r['fds']} | {r['state']} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30.0, help="total duration")
    ap.add_argument("--interval", type=float, default=15.0, help="seconds between samples")
    ap.add_argument("--ask-every", type=float, default=0.0,
                    help="send a request this many seconds (costs quota)")
    ap.add_argument("--ask", default="what is the capital of Iraq? one short sentence")
    ap.add_argument("--out", default=None, help="write the markdown report here")
    args = ap.parse_args()

    start = time.time()
    end = start + args.minutes * 60
    rows: list[dict] = []
    freezes: list[str] = []
    last_cpu: float | None = None
    last_t = start
    next_ask = start + args.ask_every if args.ask_every else None
    phase = "idle"

    print(f"soaking for {args.minutes:g} min, sampling every {args.interval:g}s "
          f"(ctrl-c to stop and report)", flush=True)
    try:
        while time.time() < end:
            s = sample()
            if not s.get("alive"):
                freezes.append(f"t={time.time() - start:.0f}s: service not running")
                print("!! service is not running", flush=True)
                time.sleep(args.interval)
                continue
            now = time.time()
            cpu_pct = 0.0
            if last_cpu is not None and now > last_t:
                cpu_pct = max(0.0, (s["cpu_s"] - last_cpu) / (now - last_t) * 100.0)
            last_cpu, last_t = s["cpu_s"], now
            s["elapsed"] = f"{now - start:.0f}s"
            s["phase"] = phase
            s["cpu_pct"] = cpu_pct
            rows.append(s)
            print(f"[{s['elapsed']:>6}] {phase:<8} daemon={s['daemon_mb']:6.1f} "
                  f"hud={s['hud_mb']:6.1f} total={s['total_mb']:6.1f} MiB  "
                  f"cpu={cpu_pct:5.2f}%  fds={s['fds']}", flush=True)

            if next_ask and now >= next_ask:
                next_ask = now + args.ask_every
                print(f"  -> ask: {args.ask!r}", flush=True)
                r = ctl("ask", text=args.ask, no_speak=True)
                print(f"     answer: {str(r.get('answer'))[:90]}", flush=True)
                time.sleep(2)
                continue

            # Exercise the model lifecycle so we measure both ends of it:
            # idle -> warm (whisper+piper loaded) -> idle again (unloaded).
            cycle = (now - start) % max(args.interval * 8, 60.0)
            if phase == "idle" and args.minutes * 60 > 60 and cycle > 30:
                r = ctl("warm")
                if r.get("ok"):
                    phase = "warm"
                    print(f"  -> warm: daemon={r['rss_mb']:.1f} MiB "
                          f"(+{r.get('delta_mb', 0):.1f})", flush=True)

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped early", flush=True)

    if not rows:
        print("no samples collected")
        return 1

    idle = [r for r in rows if r["phase"] == "idle"]
    warm = [r for r in rows if r["phase"] == "warm"]
    report = ["# Alpha soak test", "",
              f"- duration: {(time.time() - start) / 60:.1f} min, "
              f"{len(rows)} samples every {args.interval:g}s",
              f"- samples: idle={len(idle)} warm={len(warm)}", ""]
    for name, group in (("idle", idle), ("warm", warm)):
        if not group:
            continue
        report += [f"## {name} (n={len(group)})", "",
                   f"- total RSS: first {group[0]['total_mb']:.1f} MiB, "
                   f"avg {sum(g['total_mb'] for g in group) / len(group):.1f} MiB, "
                   f"max {max(g['total_mb'] for g in group):.1f} MiB",
                   f"- daemon: avg {sum(g['daemon_mb'] for g in group) / len(group):.1f} MiB, "
                   f"HUD: avg {sum(g['hud_mb'] for g in group) / len(group):.1f} MiB",
                   f"- CPU: avg {sum(g['cpu_pct'] for g in group) / len(group):.2f}%, "
                   f"max {max(g['cpu_pct'] for g in group):.2f}%",
                   f"- fds: {group[0]['fds']} -> {group[-1]['fds']}, "
                   f"threads: {group[0]['threads']} -> {group[-1]['threads']}", ""]
    if idle and len(idle) > 2:
        drift = idle[-1]["total_mb"] - idle[0]["total_mb"]
        report += ["## leak check", "",
                   f"- idle RSS drift over the run: {drift:+.1f} MiB "
                   f"({idle[0]['total_mb']:.1f} -> {idle[-1]['total_mb']:.1f})",
                   f"- fd drift: {idle[-1]['fds'] - idle[0]['fds']:+d}, "
                   f"thread drift: {idle[-1]['threads'] - idle[0]['threads']:+d}", ""]
    report += ["## failures", ""]
    report += [f"- {f}" for f in freezes] if freezes else ["- none"]
    report += [""]
    report += ["## samples", "", fmt_table(rows), ""]

    text = "\n".join(report)
    if args.out:
        Path(args.out).write_text(text)
        print(f"report written to {args.out}", flush=True)
    print("\n" + text[-2500:], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
