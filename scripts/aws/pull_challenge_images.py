"""Pull the challenge-server images for every challenge in the Phase 1 subset.

Images must be local before experiments: the agent network is internal, and pulling
during an attempt would count against the attempt's time.
"""
import subprocess
from pathlib import Path

import yaml
from nyuctf.dataset import CTFDataset

REPO = Path(__file__).resolve().parents[2]
spec = yaml.safe_load((REPO / "experiments/phase1/challenges.yaml").read_text())

for set_name in ("pilot", "development", "heldout"):
    dataset = CTFDataset(split=spec[set_name]["split"])
    for c in spec[set_name]["challenges"]:
        compose = Path(dataset.basedir) / dataset.get(c["id"])["path"] / "docker-compose.yml"
        if compose.exists():
            r = subprocess.run(["docker", "compose", "-f", str(compose), "pull", "-q"], capture_output=True, text=True)
            print(f"{set_name}/{c['id']}: {'ok' if r.returncode == 0 else 'FAILED ' + r.stderr.strip()[-200:]}", flush=True)
