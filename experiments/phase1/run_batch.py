#!/usr/bin/env python3
"""Run a set of Phase 1 attempts against the vLLM server, a few at a time.

  python experiments/phase1/run_batch.py --set pilot --attempts 1
  python experiments/phase1/run_batch.py --set development --attempts 3 --parallel 3

Attempts already logged are skipped, so the same command resumes an interrupted batch.
Two attempts never run at once if their challenge servers share a hostname alias or a
host port, since they would collide on the shared Docker network. SIGTERM (for example
from the auto-stop) is forwarded to running attempts so each one saves its log.
"""
import argparse
import json
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import yaml
from nyuctf.dataset import CTFDataset

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def server_keys(chal_dir: Path):
    """Hostname aliases and host ports a challenge's docker-compose file claims."""
    compose = chal_dir / "docker-compose.yml"
    if not compose.exists():
        return set()
    keys = set()
    for svc in (yaml.safe_load(compose.read_text()) or {}).get("services", {}).values():
        for net in (svc.get("networks") or {}).values() if isinstance(svc.get("networks"), dict) else []:
            keys.update(f"alias:{a}" for a in (net or {}).get("aliases", []))
        for p in svc.get("ports", []):
            keys.add(f"port:{str(p).split(':')[0]}")
    return keys


def preflight(network, endpoint, model):
    internal = subprocess.run(["docker", "network", "inspect", "-f", "{{.Internal}}", network],
                              capture_output=True, text=True).stdout.strip()
    if internal != "true":
        sys.exit(f"Docker network {network!r} is not internal; run ./setup.sh first")
    try:
        served = json.load(urllib.request.urlopen(f"{endpoint}/models", timeout=10))
    except Exception as e:
        sys.exit(f"vLLM server not reachable at {endpoint}: {e}")
    names = [m["id"] for m in served.get("data", [])]
    if model not in names:
        sys.exit(f"vLLM serves {names}, not {model}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", required=True, choices=["pilot", "development", "heldout"])
    ap.add_argument("--attempts", type=int, default=1, help="attempts per challenge")
    ap.add_argument("--first-attempt", type=int, default=0)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--only", nargs="+", help="restrict to these challenge ids")
    ap.add_argument("--config", type=Path, default=HERE / "qwen_phase1.yaml")
    ap.add_argument("--challenges", type=Path, default=HERE / "challenges.yaml")
    ap.add_argument("--logroot", type=Path, default=REPO / "logs" / "phase1")
    ap.add_argument("--endpoint", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--network", default="ctfnet")
    args = ap.parse_args()

    config = yaml.safe_load(args.config.read_text())
    name = config["experiment"]["name"]
    preflight(args.network, args.endpoint, config["parameter"]["model"])

    spec = yaml.safe_load(args.challenges.read_text())[args.set]
    excluded = {e["id"] for e in yaml.safe_load(args.challenges.read_text()).get("excluded") or []}
    split = spec["split"]
    dataset = CTFDataset(split=split)
    chals = [c["id"] for c in spec["challenges"] if c["id"] not in excluded]
    if args.only:
        chals = [c for c in chals if c in args.only]
    keys = {c: server_keys(Path(dataset.basedir) / dataset.get(c)["path"]) for c in chals}

    logdir = args.logroot / args.set
    consoledir = logdir / "console"
    consoledir.mkdir(parents=True, exist_ok=True)
    # Attempt-major order spreads each challenge's attempts apart in time
    pending = [(i, c) for i in range(args.first_attempt, args.first_attempt + args.attempts) for c in chals
               if not (logdir / f"{name}_round{i}" / f"{c}.json").exists()]
    print(f"{len(pending)} attempts to run ({args.set}, parallel={args.parallel})", flush=True)

    running = {}  # Popen -> (attempt, challenge, start)
    stopping = False

    def forward(signum, frame):
        nonlocal stopping
        stopping = True
        for p in running:
            p.send_signal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    t0 = time.time()
    done = 0
    while (pending and not stopping) or running:
        # Start attempts whose challenge and server names are free
        while pending and not stopping and len(running) < args.parallel:
            busy_chals = {c for _, c, _ in running.values()}
            busy_keys = set().union(*(keys[c] for c in busy_chals)) if busy_chals else set()
            job = next((j for j in pending if j[1] not in busy_chals and not (keys[j[1]] & busy_keys)), None)
            if job is None:
                break
            pending.remove(job)
            i, c = job
            cmd = [sys.executable, str(REPO / "llm_ctf_solve.py"), "--challenge", c, "--split", split,
                   "-c", str(args.config), "-L", str(logdir), "-n", name, "-i", str(i), "-q",
                   "--api-endpoint", args.endpoint, "-N", args.network]
            out = open(consoledir / f"{c}_round{i}.log", "w")
            running[subprocess.Popen(cmd, cwd=REPO, stdout=out, stderr=subprocess.STDOUT)] = (i, c, time.time())
            print(f"[{time.time() - t0:7.0f}s] start {c} #{i}", flush=True)
        time.sleep(2)
        for p in [p for p in running if p.poll() is not None]:
            i, c, st = running.pop(p)
            done += 1
            log = logdir / f"{name}_round{i}" / f"{c}.json"
            result = json.loads(log.read_text())["finish_reason"] if log.exists() else "NO LOG"
            print(f"[{time.time() - t0:7.0f}s] done  {c} #{i}: {result} in {time.time() - st:.0f}s "
                  f"(exit {p.returncode}; {done} done, {len(pending)} left)", flush=True)
    print(f"batch finished in {(time.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
