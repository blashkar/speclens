"""Compare speclens's diff with oasdiff's on two specs: which operations each one says break.

    uv run python tools/compare_with_oasdiff.py OLD_SPEC NEW_SPEC [--show 20]

Needs `oasdiff` on the PATH (https://github.com/oasdiff/oasdiff). It runs with --flatten-allof, since speclens
merges allOf branches too; without it oasdiff compares branches one at a time. oasdiff is an independent tool;
where the two disagree, both reasons are printed so the disagreement can be judged by reading the specs.
Reads the specs in place and writes nothing.
"""

import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict

from speclens import SpecSet, load_spec

ERR = {3, "error", "ERR"}


def oasdiff_breaking(old: str, new: str) -> dict[str, list[str]]:
    """operation ("METHOD /path") -> oasdiff's error-level messages."""
    run = subprocess.run(["oasdiff", "breaking", old, new, "--format", "json", "--flatten-allof"],
                         capture_output=True, text=True)
    if run.returncode not in (0, 1):
        sys.exit(f"oasdiff failed: {run.stderr.strip()}")
    out = defaultdict(list)
    for item in json.loads(run.stdout or "[]"):
        if item.get("level") in ERR:
            op = f"{item.get('operation', '').upper()} {item.get('path', '')}".strip()
            out[op].append(f"{item.get('id')}: {item.get('text', '')}")
    return out


def speclens_breaking(old: str, new: str) -> dict[str, list[str]]:
    specs = SpecSet()
    specs.add(load_spec(old, "old"))
    specs.add(load_spec(new, "new"))
    out = defaultdict(list)
    for op, change in specs.diff("old", "new").breaking():
        out[op].append(change.line())
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("old")
    parser.add_argument("new")
    parser.add_argument("--show", type=int, default=15, help="disagreements to print on each side")
    args = parser.parse_args(argv)
    if not shutil.which("oasdiff"):
        sys.exit("oasdiff is not on the PATH")
    theirs, ours = oasdiff_breaking(args.old, args.new), speclens_breaking(args.old, args.new)
    both = set(theirs) & set(ours)
    only_theirs, only_ours = sorted(set(theirs) - set(ours)), sorted(set(ours) - set(theirs))
    print(f"operations with breaking changes: oasdiff {len(theirs)}, speclens {len(ours)}, both {len(both)}")
    print(f"only oasdiff: {len(only_theirs)}, only speclens: {len(only_ours)}")
    for title, ops, mine, other in (("only oasdiff", only_theirs, theirs, ours), ("only speclens", only_ours, ours, theirs)):
        if ops:
            print(f"\n{title}:")
        for op in ops[:args.show]:
            print(f"  {op}")
            for reason in mine[op][:3]:
                print(f"    {reason}")
    return 0 if not only_theirs and not only_ours else 1


if __name__ == "__main__":
    sys.exit(main())
