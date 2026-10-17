#!/usr/bin/env python3
"""Extract a compact paired table of per-branch-type MPKI."""

import argparse
import csv
from pathlib import Path


BRANCH_TYPES = ("conditional", "direct", "indirect", "indirect_retcall", "call", "ret")


def read_points(path):
    with path.open(newline="") as source:
        return {(r["benchmark"], r["point"]): r for r in csv.DictReader(source) if r.get("ipc", "")}


def number(row, field):
    value = row.get(field, "")
    return float(value) if value != "" else None


def fmt(value):
    return "" if value is None else f"{value:.12g}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--execution", required=True, type=Path)
    parser.add_argument("--suite", required=True)
    parser.add_argument("-o", "--output", required=True, type=Path)
    args = parser.parse_args()

    trace = read_points(args.trace)
    execution = read_points(args.execution)
    fields = ["suite", "benchmark", "point", "weight"]
    for branch_type in BRANCH_TYPES:
        fields.extend((
            f"{branch_type}_trace_count",
            f"{branch_type}_execution_count",
            f"{branch_type}_trace_mispredicts",
            f"{branch_type}_execution_mispredicts",
            f"{branch_type}_trace_mpki",
            f"{branch_type}_execution_mpki",
            f"{branch_type}_trace_minus_execution_mpki",
        ))

    rows = []
    for benchmark, point in sorted(trace.keys() & execution.keys()):
        t, e = trace[(benchmark, point)], execution[(benchmark, point)]
        row = {"suite": args.suite, "benchmark": benchmark, "point": point, "weight": t.get("weight", "")}
        for branch_type in BRANCH_TYPES:
            for mode, source in (("trace", t), ("execution", e)):
                row[f"{branch_type}_{mode}_count"] = source.get(f"{branch_type}_branch_count", "")
                row[f"{branch_type}_{mode}_mispredicts"] = source.get(
                    f"{branch_type}_branch_mispredicts", ""
                )
                row[f"{branch_type}_{mode}_mpki"] = source.get(f"{branch_type}_branch_mpki", "")
            t_mpki = number(t, f"{branch_type}_branch_mpki")
            e_mpki = number(e, f"{branch_type}_branch_mpki")
            row[f"{branch_type}_trace_minus_execution_mpki"] = (
                fmt(t_mpki - e_mpki) if t_mpki is not None and e_mpki is not None else ""
            )
        rows.append(row)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} paired points to {args.output}")


if __name__ == "__main__":
    main()
