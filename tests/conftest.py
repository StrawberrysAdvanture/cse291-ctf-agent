"""Test setup: the backends read keys.cfg at import time, so provide a temporary one."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

KEYS = ROOT / "keys.cfg"
_created = False
if not KEYS.exists():
    KEYS.write_text("MODEL_URL: http://127.0.0.1:9/v1\n")
    _created = True


def pytest_sessionfinish(session, exitstatus):
    if _created and KEYS.exists():
        KEYS.unlink()
