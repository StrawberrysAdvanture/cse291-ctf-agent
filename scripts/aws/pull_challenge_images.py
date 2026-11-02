"""Pull the challenge-server images for every challenge in the Phase 1 subset.

Images must be local before experiments: the agent network is internal, and pulling
during an attempt would count against the attempt's time.

Also deconflicts host ports: vLLM listens on host port 8000, and some web
challenges (e.g. 2023f-web-shreeramquest) publish their server on host 8000,
so `docker compose up` fails with "address already in use" and the attempt
records a setup_error. The agent reaches the challenge over the internal
ctfnet network (by its `box` hostname / internal_port), never via the host
port, so remapping the host side is behavior-neutral. Re-run on a fresh
dataset download reapplies it.
"""
import re
import subprocess
from pathlib import Path

import yaml
from nyuctf.dataset import CTFDataset

REPO = Path(__file__).resolve().parents[2]
spec = yaml.safe_load((REPO / "experiments/phase1/challenges.yaml").read_text())

VLLM_HOST_PORT = "8000"
REMAP_HOST_PORT = "18000"
# Match a compose short-form port mapping whose host side is 8000, e.g. "8000:80"
# or "127.0.0.1:8000:80", in quoted or bare form. Only the host port is rewritten.
_HOST_PORT = re.compile(rf'(^\s*-\s*["\']?(?:\d+\.\d+\.\d+\.\d+:)?){VLLM_HOST_PORT}(:)', re.M)


def deconflict_ports(compose: Path) -> bool:
    text = compose.read_text()
    new = _HOST_PORT.sub(rf'\g<1>{REMAP_HOST_PORT}\g<2>', text)
    if new != text:
        compose.write_text(new)
        return True
    return False


for set_name in ("pilot", "development", "heldout"):
    dataset = CTFDataset(split=spec[set_name]["split"])
    for c in spec[set_name]["challenges"]:
        compose = Path(dataset.basedir) / dataset.get(c["id"])["path"] / "docker-compose.yml"
        if compose.exists():
            if deconflict_ports(compose):
                print(f"{set_name}/{c['id']}: remapped host port {VLLM_HOST_PORT}->{REMAP_HOST_PORT}", flush=True)
            r = subprocess.run(["docker", "compose", "-f", str(compose), "pull", "-q"], capture_output=True, text=True)
            print(f"{set_name}/{c['id']}: {'ok' if r.returncode == 0 else 'FAILED ' + r.stderr.strip()[-200:]}", flush=True)
