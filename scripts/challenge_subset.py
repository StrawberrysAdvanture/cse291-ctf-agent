#!/usr/bin/env python3
"""Pick a category-stratified subset of challenge names.

Usage:
    python3 scripts/challenge_subset.py FULL_LIST SIZE > config/challenges/dev-subset-SIZE.txt

FULL_LIST has one challenge name per line; blank lines and lines starting with # are
ignored. The category is the second dash-separated token of the name (2016q-cry-katy
-> cry). SIZE is split across categories in proportion to their size (largest-remainder
method, ties broken alphabetically), and within each category the names are sorted and
picked at evenly spaced positions. The output is deterministic: the same input and SIZE
always give the same list. A SIZE below the number of categories drops categories.
The per-category shares are printed to stderr. Standard library only.
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path


def read_names(path):
    names = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names


def category(name):
    parts = name.split("-")
    return parts[1] if len(parts) > 1 else "?"


def apportion(sizes, total):
    """Largest-remainder apportionment of `total` seats over {category: count}."""
    n = sum(sizes.values())
    if total > n:
        raise ValueError(f"SIZE {total} is larger than the list ({n} names)")
    take = {c: k * total // n for c, k in sizes.items()}
    remainder = {c: (k * total / n) - take[c] for c, k in sizes.items()}
    left = total - sum(take.values())
    for c in sorted(sizes, key=lambda c: (-remainder[c], c))[:left]:
        take[c] += 1
    return take


def subset(names, size):
    groups = defaultdict(list)
    for name in names:
        groups[category(name)].append(name)
    take = apportion({c: len(g) for c, g in groups.items()}, size)
    chosen = []
    for cat in sorted(groups):
        members = sorted(groups[cat])
        k = take[cat]
        chosen.extend(members[int((i + 0.5) * len(members) / k)] for i in range(k))
    shares = {c: (take[c], len(groups[c])) for c in sorted(groups)}
    return chosen, shares


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("full_list", help="file with one challenge name per line")
    parser.add_argument("size", type=int, help="number of challenges to pick")
    args = parser.parse_args()
    names = read_names(args.full_list)
    if args.size < 1:
        parser.error("SIZE must be at least 1")
    try:
        chosen, shares = subset(names, args.size)
    except ValueError as e:
        parser.error(str(e))
    print(f"# {len(chosen)} of {len(names)} development challenges, stratified by category; "
          f"regenerate with: python3 scripts/challenge_subset.py {args.full_list} {args.size}")
    for name in chosen:
        print(name)
    print(", ".join(f"{c} {k}/{n}" for c, (k, n) in shares.items()), file=sys.stderr)


if __name__ == "__main__":
    main()
