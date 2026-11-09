"""Check speclens on a random sample of public APIs from the APIs.guru directory.

    uv run python tools/sample_public_apis.py OUT_DIR [--count 120] [--seed 1] [--all-31]

Downloads OpenAPI 3.x specs from https://api.apis.guru/v2/list.json into OUT_DIR (keep it outside the repo:
the specs have many licenses), then runs tools/crosscheck.py on each, in one process, and prints a summary:
how many pass, crash or differ, and which issues speclens reported. `--all-31` adds every OpenAPI 3.1 spec
in the directory, since there are few of them. A re-run reuses what was downloaded.
"""

import argparse
import collections
import contextlib
import importlib.util
import io
import json
import random
import sys
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

LIST = "https://api.apis.guru/v2/list.json"


def fetch(url: str) -> bytes:
    url = urllib.parse.quote(url, safe=":/?&=%")  # some listed paths contain spaces
    request = urllib.request.Request(url, headers={"User-Agent": "speclens-sample/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--count", type=int, default=120)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--all-31", action="store_true")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    apis = json.loads(fetch(LIST))
    pool = []
    for name, api in sorted(apis.items()):
        version = api["versions"][api["preferred"]]
        if str(version.get("openapiVer", "")).startswith("3.") and version.get("swaggerYamlUrl"):
            pool.append((name, str(version["openapiVer"]), version["swaggerYamlUrl"]))
    chosen = random.Random(args.seed).sample(pool, min(args.count, len(pool)))
    if args.all_31:
        chosen += [p for p in pool if p[1].startswith("3.1") and p not in chosen]
    print(f"{len(pool)} OpenAPI 3.x specs listed; checking {len(chosen)}")

    here = Path(__file__).parent
    spec = importlib.util.spec_from_file_location("crosscheck", here / "crosscheck.py")
    crosscheck = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(crosscheck)
    from speclens import load_spec

    results = collections.Counter()
    issues = collections.Counter()
    for name, _, url in chosen:
        path = args.out / (name.replace("/", "_").replace(":", "_") + ".yaml")
        if not path.exists():
            try:
                path.write_bytes(fetch(url))
            except Exception as e:
                print(f"skip {name}: {e}")
                continue
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                problems = crosscheck.check(path)
            loaded = load_spec(path, "x")
            issues.update({i.code for i in loaded.issues})
            results["ok" if not problems else "differences"] += 1
            if problems:
                print(f"differences in {name}: {problems[:2]}")
        except Exception:
            results["crash"] += 1
            print(f"crash in {name}: {traceback.format_exc().strip().splitlines()[-1]}")
        crosscheck._cache.clear()
    print(dict(results))
    print("issues speclens reported (specs with each):", dict(issues))
    return 0 if set(results) <= {"ok"} else 1


if __name__ == "__main__":
    sys.exit(main())
