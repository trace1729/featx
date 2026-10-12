#!/usr/bin/env python3
"""Print a collector points.csv for a reference, collecting it first if needed.

REF may be: a points.csv file, a collector output dir (has points.csv), or a root of
<bench>_<cp>_<weight> point dirs. Raw roots are collected with collect_gsim_spec_perf.py
(-N 2, restricted to --config points) into $FEATX_ROOT/compare/collect-cache/<key>/, reused
while the inputs are unchanged.  stdlib only (runs on any node)."""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ref")
    ap.add_argument("--config", required=True)
    ap.add_argument("--suite", default="spec06")
    ap.add_argument("-N", type=int, default=2)
    args = ap.parse_args()
    ref = Path(args.ref)
    if ref.is_file() and ref.suffix == ".csv":
        print(ref)
        return 0
    if (ref / "points.csv").is_file() and not any(ref.glob("*_*_*/simulator_out.txt")):
        print(ref / "points.csv")
        return 0
    if not ref.is_dir():
        print(f"ensure_collected: reference not found: {ref}", file=sys.stderr)
        return 2
    cfg = Path(args.config).read_text()
    points = json.loads(cfg)
    stamp = []
    for bench, info in sorted(points.items()):
        for cp, w in sorted(info["points"].items()):
            d = ref / f"{bench}_{cp}_{w}"
            for name in ("simulator_err.txt", "simulator_out.txt"):
                try:
                    st = (d / name).stat()
                    stamp.append(f"{d.name}/{name}:{st.st_size}:{int(st.st_mtime)}")
                except FileNotFoundError:
                    pass
    key = hashlib.sha256(json.dumps([str(ref.resolve()), cfg, args.suite, args.N, stamp]).encode()).hexdigest()[:16]
    root = Path(os.environ.get("FEATX_ROOT", "/nfs/share/gkc/featx"))
    out = root / "compare" / "collect-cache" / key
    out.mkdir(parents=True, exist_ok=True)
    with open(out / ".lock", "w") as lock:
        fcntl.lockf(lock, fcntl.LOCK_EX)
        if not (out / "points.csv").is_file():
            (out / "config.json").write_text(cfg)
            (out / "source").write_text(str(ref.resolve()) + "\n")
            tools = os.environ.get("FEATX_TOOLS",
                                   "/nfs/home/gongkaichen/tracertl/toolsets/microarch/spec_trace_compare/03_analysis")
            proc = subprocess.run([sys.executable, f"{tools}/collect_gsim_spec_perf.py", "-r", str(ref),
                                   "--config", str(out / "config.json"), "--suite", args.suite,
                                   "-N", str(args.N), "-o", str(out)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if proc.returncode != 0:
                (out / "points.csv").unlink(missing_ok=True)
                print(f"ensure_collected: collector failed on {ref}:\n{proc.stderr[-3000:]}", file=sys.stderr)
                return proc.returncode
    print(out / "points.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
