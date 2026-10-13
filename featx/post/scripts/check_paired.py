#!/usr/bin/env python3
"""Refuse a trace/reference comparison that has nothing to pair, with a message that says why.

  check_paired.py TRACE_POINTS_CSV REF_POINTS_CSV

Both are collector points.csv files. A point is valid when its ipc column is set, and points
pair on (benchmark, point): the same rules as compare_gsim_spec_points.py, which otherwise dies
with ZeroDivisionError (diagnose_top_ipc_gaps.py: RuntimeError; extract_branch_type_mpki.py
writes an empty table). Exit 0 with a one-line count on stderr, 3 if no point pairs.  stdlib only."""

import csv
import sys
from collections import Counter


def load(path):
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    valid = {(r.get("benchmark"), r.get("point")) for r in rows if r.get("ipc")}
    why = Counter(r.get("status") or "no ipc" for r in rows if not r.get("ipc"))
    return rows, valid, why


def describe(label, path, rows, valid, why):
    text = f"{label} {path}: {len(valid)}/{len(rows)} points valid"
    if why:
        text += " (invalid: " + ", ".join(f"{s} x{n}" for s, n in why.most_common(4)) + ")"
    return text


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    trace_path, ref_path = sys.argv[1:]
    trace = load(trace_path)
    ref = load(ref_path)
    common = trace[1] & ref[1]
    lines = [describe("trace", trace_path, *trace), describe("reference", ref_path, *ref),
             f"paired: {len(common)}"]
    if common:
        print("check_paired: " + "; ".join(lines), file=sys.stderr)
        return 0
    hint = ("the runset has no valid point: did the run fail, or is I < W (no formal-window PERF block)?"
            if not trace[1] else
            "the reference has no valid point" if not ref[1] else
            "no valid point of the runset is valid in the reference: other point set / reference?")
    print("check_paired: nothing to compare: " + hint, *lines, sep="\n  ", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
