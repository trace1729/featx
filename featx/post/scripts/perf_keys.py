#!/usr/bin/env python3
"""Formal-window key PERF counters per point across runs (generalized 1007/1009
summarize.py + compare-panel.py).

  perf_keys.py --runs exec=/root/a self=/root/b ... --points points.json|points.txt
               [--block 2] [--keys extra.yaml] [--ref-first] -o OUT

Each run root holds <bench>_<cp>_<weight>/simulator_{err,out}.txt. Counters are summed over
module instances (module path suffix of 2 components, _<n> -> _N) inside the selected PERF
block (blocks are distinguished by their [time=..] stamp; 2 = formal window after warmup).
The first run is the IPC reference. Writes points.csv, report.md, summary.json.
stdlib (+ yaml only when --keys is given)."""

import argparse
import csv
import json
import math
import os
import re
import sys

KEYS = [
    ("cycles", "ctrlBlock.rob", "clock_cycle"),
    ("insts", "ctrlBlock.rob", "commitInstr"),
    ("backend_redirects", "ctrlBlock.rob", "total_flush"),
    ("commit_mispred", "inner.ftq", "commit_branch_mispredicts"),
    ("ifu_wb_redirect", "ifu.perfAnalyzer", "fetch_flush_wb_redirect"),
    ("l2_demand_miss", "l2cache.topDown", "l2demandMiss"),
    ("mem_stall_l2miss", "inner.topDownMod", "mem_stall_l2miss"),
    ("ctrl_redirect_stall", "ctrlBlock.dispatch", "ControlRedirectStall"),
    ("wpe_enter", "ifu.traceRTL", "wpe_enter"),
    ("wpe_active_cycles", "ifu.traceRTL", "wpe_active_cycles"),
    ("wpe_enq_insts", "ifu.traceRTL", "wpe_enq_insts"),
    ("l1d_mlp_sum", "dcache.missQueue", "L1DMLP_CPUData_sum"),
]
PERF = re.compile(r"\[PERF \]\[time=\s*(\d+)\] (\S+): (\S+),\s+(-?\d+)")


def load_points(path):
    text = open(path).read()
    if path.endswith(".json"):
        data = json.loads(text)
        return [f"{b}_{cp}_{w}" for b, info in data.items() for cp, w in info["points"].items()]
    return [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]


def scan(point_dir, want, block):
    blocks = {}
    for fn in ("simulator_err.txt", "simulator_out.txt"):
        p = os.path.join(point_dir, fn)
        if not os.path.isfile(p):
            continue
        with open(p, errors="ignore") as handle:
            for line in handle:
                if not line.startswith("[PERF"):
                    continue
                m = PERF.match(line)
                if not m:
                    continue
                mod = re.sub(r"_\d+", "_N", m.group(2))
                k = want.get((".".join(mod.split(".")[-2:]), m.group(3)))
                if k:
                    b = blocks.setdefault(int(m.group(1)), {})
                    b[k] = b.get(k, 0) + int(m.group(4))
    ts = sorted(blocks)
    if len(ts) < block:
        # I < W ends the run before the formal window: only the final dump exists
        return f"{len(ts)} PERF block(s) < {block}" if ts else "no PERF output"
    return blocks[ts[block - 1]]


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True, help="label=root ...")
    ap.add_argument("--points", required=True)
    ap.add_argument("--block", type=int, default=2)
    ap.add_argument("--keys", default="")
    ap.add_argument("--ref-first", action="store_true", help="(default) first run is the IPC reference")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()
    keys = list(KEYS)
    if args.keys:
        import yaml
        keys += [tuple(k) for k in yaml.safe_load(open(args.keys))]
    want = {(m, c): k for k, m, c in keys}
    runs = []
    for spec in args.runs:
        label, _, root = spec.partition("=")
        if not root:
            sys.exit(f"perf_keys: bad --runs entry {spec!r} (want label=root)")
        runs.append((label, root))
    points = load_points(args.points)
    os.makedirs(args.out, exist_ok=True)
    names = [k for k, _, _ in keys]
    rows, errs, ipcs = [], {}, {}
    for p in points:
        ref_ipc = None
        for i, (label, root) in enumerate(runs):
            v = scan(os.path.join(root, p), want, args.block)
            row = {"point": p, "run": label}
            if isinstance(v, str) or not v.get("cycles"):
                row["status"] = "missing"
                row["reason"] = v if isinstance(v, str) else "no cycles counter"
                rows.append(row)
                continue
            ipc = v.get("insts", 0) / v["cycles"]
            if i == 0:
                ref_ipc = ipc
            err = (ipc / ref_ipc - 1) * 100 if ref_ipc and i > 0 else None
            row.update(status="ok", ipc=round(ipc, 6), ipc_err_pct=None if err is None else round(err, 4),
                       mlp=round(v.get("l1d_mlp_sum", 0) / v["cycles"], 4))
            row.update({k: v.get(k, 0) for k in names})
            rows.append(row)
            ipcs.setdefault(label, {})[p] = ipc
            if err is not None:
                errs.setdefault(label, {})[p] = err
    fields = ["point", "run", "status", "reason", "ipc", "ipc_err_pct", "mlp"] + names
    with open(os.path.join(args.out, "points.csv"), "w", newline="") as handle:
        w = csv.DictWriter(handle, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    labels = [l for l, _ in runs]
    others = labels[1:]
    common = [p for p in points if all(p in errs.get(l, {}) for l in others)] if others else \
        [p for p in points if p in ipcs.get(labels[0], {})]
    summary = {"reference": labels[0], "block": args.block, "points": len(points), "common_points": len(common)}
    for label in labels:
        got = ipcs.get(label, {})
        summary[f"{label}.finished"] = len(got)
        vals = [got[p] for p in common if p in got and got[p] > 0]
        summary[f"{label}.ipc_geomean"] = math.exp(sum(map(math.log, vals)) / len(vals)) if vals else None
        if label in errs:
            summary[f"{label}.mean_abs_err_pct"] = mean([abs(errs[label][p]) for p in common])
            summary[f"{label}.mean_err_pct"] = mean([errs[label][p] for p in common])
            summary[f"{label}.max_abs_err_pct"] = max((abs(errs[label][p]) for p in common), default=None)
    with open(os.path.join(args.out, "summary.json"), "w") as handle:
        json.dump(summary, handle, indent=2)

    md = [f"# perf-keys (PERF block {args.block}, reference = {labels[0]})", "",
          "| point | run | IPC | err% | MLP | l2dMiss | memL2 | cmtMisp | wpEnq |", "|" + "---|" * 9]
    for r in rows:
        if r.get("status") != "ok":
            md.append(f"| {r['point']} | {r['run']} | missing: {r['reason']} | | | | | | |")
            continue
        e = "" if r["ipc_err_pct"] is None else f"{r['ipc_err_pct']:+.2f}"
        md.append(f"| {r['point']} | {r['run']} | {r['ipc']:.3f} | {e} | {r['mlp']:.2f} | {r['l2_demand_miss']} "
                  f"| {r['mem_stall_l2miss']} | {r['commit_mispred']} | {r['wpe_enq_insts']} |")
    md += ["", f"Common points (every run finished): {len(common)}", ""]
    for label in others if common else []:
        md.append(f"- {label}: mean|err| {summary.get(f'{label}.mean_abs_err_pct') or 0:.2f}%  "
                  f"mean err {summary.get(f'{label}.mean_err_pct') or 0:+.2f}%")
    if others and not common:
        md.append("- no point finished in every run: no error statistics")
    with open(os.path.join(args.out, "report.md"), "w") as handle:
        handle.write("\n".join(md) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
