#!/usr/bin/env python3
"""csv_summary.py <csv> <summary.json> [--prefix P]: flat summary of a CSV (row count and
mean of every numeric column), merged into summary.json if it already exists."""

import argparse
import csv
import json
import os


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("summary")
    ap.add_argument("--prefix", default="")
    args = ap.parse_args()
    pre = f"{args.prefix}." if args.prefix else ""
    with open(args.csv, newline="") as handle:
        rows = list(csv.DictReader(handle))
    out = json.load(open(args.summary)) if os.path.isfile(args.summary) else {}
    out[f"{pre}rows"] = len(rows)
    cols: dict[str, list[float]] = {}
    for row in rows:
        for key, value in row.items():
            try:
                cols.setdefault(key, []).append(float(value))
            except (TypeError, ValueError):
                pass
    for key, vals in cols.items():
        if vals:
            out[f"{pre}mean.{key}"] = sum(vals) / len(vals)
    with open(args.summary, "w") as handle:
        json.dump(out, handle, indent=2)


if __name__ == "__main__":
    main()
