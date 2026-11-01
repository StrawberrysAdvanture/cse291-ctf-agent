"""Write sanitized copies of attempt logs: answer-key flags replaced, local paths removed.

Usage: python analysis/sanitize.py LOGROOT DATASET_DIR OUTDIR
"""
import argparse
import json
import re
from pathlib import Path

from traces import load_attempts, true_flag

HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s\"']+")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("logroot", type=Path)
    ap.add_argument("dataset_dir", type=Path)
    ap.add_argument("outdir", type=Path)
    args = ap.parse_args()

    n = 0
    for a in load_attempts(args.logroot):
        flag = true_flag(args.dataset_dir, a)
        text = a.path.read_text()
        # Flags can appear JSON-escaped inside tool output strings, so replace both forms
        for form in {flag, json.dumps(flag)[1:-1]}:
            text = text.replace(form, "<FLAG>")
        text = HOME_PATH.sub("<HOME>", text)
        assert flag not in text
        out = args.outdir / a.set / a.path.parent.name / a.path.name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        n += 1
    print(f"sanitized {n} logs into {args.outdir}")


if __name__ == "__main__":
    main()
