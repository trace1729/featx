#!/usr/bin/env python3
"""SPEC score per run, computed the way perf_trigger `--report` computes it
(perf_trigger/main.py report() + modules/gcpt.py, 1010 copy):

  spec_score.py --runs self=/root [label=/root ...] --points points.json|points.txt
                --insts cluster.json [--version auto|06|17|26] [--label spec06]
                [--frequency 3.0] -o OUT

  point     counted only when finished: simulator_out.txt first shows "EXCEEDING CYCLE/INSTR
            LIMIT" or "GOOD TRAP" (an "ABORT at pc" / "FATAL:" / "Error:" line before it, or an
            "Assertion failed" in stderr when stdout has no end marker, makes it aborted);
            cpi = clock_cycle / commitInstr, the last value of each bare counter name in stderr
  benchmark time = (sum cpi*w / sum w_done) * insts / (f * 1e9)       (w_done: finished points)
  group     every benchmark whose name starts with the group (spec06 "astar" -> astar_biglakes,
            astar_rivers); run_time = sum time, coverage = sum(w_done*insts) / sum insts,
            score = ref_time / run_time / f
  suite     SPECint/SPECfp/SPEC per GHz = geomean of the group scores; @f GHz = per GHz * f

Unlike perf_trigger, a group that matches no benchmark under its full name (500.perlbench_r)
is also tried as perlbench_r and perlbench, so spec17 short benchmark names (perlbench_diffmail)
score instead of giving NaN. A group with no finished point is listed in missing_groups and
its suite scores are null (perf_trigger prints nan); spec_per_ghz_available is the geomean
over the groups that did score (e.g. spec17 v1 traces have no x264/wrf), groups_scored their
count. The point set is the runset's; insts come from the insts json ({bench: {insts,
points}}), and so does each point's weight when the json lists it (as perf_trigger uses it).

The first run is "self": its scores are the top-level summary keys; every other run gets
<label>.spec_per_ghz ... and <label>.score_err_pct = (self / label - 1) * 100.
Writes groups.csv, benchmarks.csv, report.md, summary.json. stdlib only."""

import argparse
import csv
import json
import math
import os
import re
import sys

from perf_keys import load_points, point_dir
from spec_reftime import NAMES, SPEC_REFTIME

PERF = re.compile(
    r"\[PERF\s*\]\[time=\s*\d+\] (([a-zA-Z0-9_]+\.)+[a-zA-Z0-9_@]+): ((\w| |\')+),\s+-?(\d+)$")
ABORT_MARKS = ("ABORT at pc", "FATAL:", "Error:")
FINISH_MARKS = ("EXCEEDING CYCLE/INSTR LIMIT", "GOOD TRAP")


def state(d):
    """perf_trigger GCPT.refresh_state: the first marker line of stdout decides."""
    out = os.path.join(d, "simulator_out.txt")
    if not os.path.isfile(out):
        return "none"
    with open(out, encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if any(m in line for m in ABORT_MARKS):
                return "aborted"
            if any(m in line for m in FINISH_MARKS):
                return "finished"
            if "SOME SIGNAL STOPS THE PROGRAM" in line:
                return "none"
    err = os.path.join(d, "simulator_err.txt")
    if os.path.isfile(err):
        with open(err, encoding="utf-8", errors="ignore") as handle:
            if any("Assertion failed" in line for line in handle):
                return "aborted"
    return "running"


def cpi(d):
    """(cpi, state); cpi is None unless finished with both counters."""
    st = state(d)
    if st != "finished":
        return None, st
    data = {}
    err = os.path.join(d, "simulator_err.txt")
    if os.path.isfile(err):
        with open(err, encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                m = PERF.match(line)
                if m and m.group(3) in ("clock_cycle", "commitInstr"):
                    data[m.group(3)] = int(m.group(5))
    if "clock_cycle" not in data or "commitInstr" not in data:
        return None, "no clock_cycle/commitInstr"
    if data["commitInstr"] == 0:
        return None, "commitInstr is 0"
    return data["clock_cycle"] / data["commitInstr"], st


def geomean(xs):
    if not xs or any(x is None or not x > 0 for x in xs):
        return None
    return math.exp(sum(map(math.log, xs)) / len(xs))


def infer_version(label, benches):
    if m := re.search(r"spec(\d\d)", label or ""):
        if m.group(1) in SPEC_REFTIME:
            return m.group(1)
    best, hits = None, 0
    for v, subs in SPEC_REFTIME.items():
        n = sum(1 for full in [*subs["int"], *subs["fp"]] if group_members(full, benches))
        if n > hits:
            best, hits = v, n
    return best


def group_members(full, benches):
    """perf_trigger matches spec06 groups by short name (astar), 17/26 by full name
    (722.palm_r); fall back to shorter spellings when nothing matches."""
    short = full.split(".", 1)[-1]
    for name in (full, short, re.sub(r"_[rs]$", "", short)):
        got = [b for b in benches if b.startswith(name)]
        if got:
            return got
    return []


def point_weight(p, weights):
    """(bench, weight, dir name): perf_trigger names result dirs <bench>_<cp>_<w:.6f> and
    computes with the insts json's own (often 7-digit) weight; points the json lacks keep the
    weight spelled in their name."""
    bench, cp, w = p.rsplit("_", 2)
    w = float(weights.get(bench, {}).get(cp, w))
    return bench, w, f"{bench}_{cp}_{w:.6f}"


def score_run(root, points, insts, weights, version, freq):
    per_bench, finished = {}, set()
    for p in points:
        bench, w, name = point_weight(p, weights)
        c, st = cpi(point_dir(root, name))
        b = per_bench.setdefault(bench, {"points": 0, "done": 0, "w_total": 0.0, "w_done": 0.0,
                                         "wcpi": 0.0, "states": {}})
        b["points"] += 1
        b["w_total"] += float(w)
        b["states"][st] = b["states"].get(st, 0) + 1
        if c is not None:
            finished.add(p)
            b["done"] += 1
            b["w_done"] += float(w)
            b["wcpi"] += c * float(w)
    for bench, b in per_bench.items():
        n = insts.get(bench)
        b["insts"] = n
        b["time"] = (b["wcpi"] / b["w_done"] * n / (freq * 1e9)) if n and b["w_done"] > 0 else None
    groups, missing = [], []
    for sub in ("int", "fp"):
        for full, ref in SPEC_REFTIME[version][sub].items():
            members = group_members(full, per_bench)
            g = {"sub": sub, "group": full, "ref_time": ref, "benchmarks": len(members),
                 "points": sum(per_bench[b]["points"] for b in members),
                 "done": sum(per_bench[b]["done"] for b in members),
                 "time": None, "score": None, "coverage": None}
            ok = members and all(per_bench[b]["time"] is not None for b in members)
            if ok:
                g["time"] = sum(per_bench[b]["time"] for b in members)
                g["coverage"] = (sum(per_bench[b]["w_done"] * per_bench[b]["insts"] for b in members)
                                 / sum(per_bench[b]["insts"] for b in members))
                g["score"] = ref / g["time"] / freq
            else:
                missing.append(full)
            groups.append(g)
    res = {"per_bench": per_bench, "groups": groups, "missing": missing}
    for key, sub in (("specint_per_ghz", "int"), ("specfp_per_ghz", "fp"), ("spec_per_ghz", None)):
        res[key] = geomean([g["score"] for g in groups if sub is None or g["sub"] == sub])
    res["spec_at_freq"] = None if res["spec_per_ghz"] is None else res["spec_per_ghz"] * freq
    covs = [g["coverage"] for g in groups if g["coverage"] is not None]
    res["min_coverage"] = min(covs) if covs else None
    scored = [g["score"] for g in groups if g["score"] is not None]
    res["spec_per_ghz_available"] = geomean(scored)
    res["groups_scored"] = len(scored)
    res["finished"] = finished
    return res


def fmt(x, spec=".3f"):
    return "nan" if x is None else format(x, spec)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True, help="label=root ... (first = self)")
    ap.add_argument("--points", required=True)
    ap.add_argument("--insts", required=True, help="json {bench: {insts, points}} (perf_trigger --json-path)")
    ap.add_argument("--version", default="auto", help="auto|06|17|26")
    ap.add_argument("--label", default="", help="workload label, used by --version auto")
    ap.add_argument("--frequency", type=float, default=3.0, help="GHz")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    runs = []
    for spec in args.runs:
        label, _, root = spec.partition("=")
        if not root:
            sys.exit(f"spec_score: bad --runs entry {spec!r} (want label=root)")
        runs.append((label, root))
    if not args.insts or not os.path.isfile(args.insts):
        sys.exit(f"spec_score: insts json {args.insts!r} not found; set with.insts "
                 "(the workload's featx.yaml `insts` key gives the default; a run.checkpoint set without "
                 "insts, e.g. take_cpt, has none)")
    raw = json.load(open(args.insts))
    insts = {b: float(v["insts"]) for b, v in raw.items() if "insts" in v}
    weights = {b: v.get("points") or {} for b, v in raw.items()}
    points = load_points(args.points)
    benches = sorted({p.rsplit("_", 2)[0] for p in points})
    unknown = [b for b in benches if b not in insts]
    if unknown:
        sys.exit(f"spec_score: no insts for {', '.join(unknown)} in {args.insts}")
    version = infer_version(args.label, benches) if args.version == "auto" else args.version.strip("spec")
    if version not in SPEC_REFTIME:
        sys.exit(f"spec_score: cannot tell the SPEC version (label {args.label!r}); set with.spec_version")
    os.makedirs(args.out, exist_ok=True)

    results = [(label, root, score_run(root, points, insts, weights, version, args.frequency)) for label, root in runs]
    finished = {label: r["finished"] for label, _, r in results}
    common = set.intersection(*finished.values()) if finished else set()

    with open(os.path.join(args.out, "groups.csv"), "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["run", "sub", "group", "benchmarks", "points", "finished", "time", "ref_time", "score", "coverage"])
        for label, _, r in results:
            for g in r["groups"]:
                w.writerow([label, g["sub"], g["group"], g["benchmarks"], g["points"], g["done"],
                            g["time"], g["ref_time"], g["score"], g["coverage"]])
    with open(os.path.join(args.out, "benchmarks.csv"), "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["run", "benchmark", "insts", "points", "finished", "weight_total", "weight_done",
                    "weighted_cpi", "time", "states"])
        for label, _, r in results:
            for bench, b in sorted(r["per_bench"].items()):
                w.writerow([label, bench, int(b["insts"]), b["points"], b["done"], round(b["w_total"], 6),
                            round(b["w_done"], 6), b["wcpi"] / b["w_done"] if b["w_done"] else None, b["time"],
                            " ".join(f"{k}:{v}" for k, v in sorted(b["states"].items()))])

    name = NAMES[version]
    self_label, _, me = results[0]
    summary = {"reference": runs[1][0] if len(runs) > 1 else self_label, "block": "last",
               "spec_version": version, "frequency": args.frequency,
               "points": len(points), "common_points": len(common),
               "missing_groups": " ".join(me["missing"]) or None}
    keys = ("spec_per_ghz", "specint_per_ghz", "specfp_per_ghz", "spec_at_freq", "min_coverage",
            "spec_per_ghz_available", "groups_scored")
    for k in keys:
        summary[k] = me[k]
    summary["finished"] = len(finished[self_label])
    for g in me["groups"]:
        summary[f"score.{g['group']}"] = g["score"]
    for label, _, r in results[1:]:
        for k in keys:
            summary[f"{label}.{k}"] = r[k]
        summary[f"{label}.finished"] = len(finished[label])
        a, b = me["spec_per_ghz"], r["spec_per_ghz"]
        summary[f"{label}.score_err_pct"] = (a / b - 1) * 100 if a and b else None
    with open(os.path.join(args.out, "summary.json"), "w") as handle:
        json.dump(summary, handle, indent=2)

    md = [f"# spec-score ({name.format('')}, {args.frequency:g} GHz, perf_trigger --report method)", ""]
    for label, root, r in results:
        md += [f"## {label}  ({root})", "", "```", f"{'':19s} {'time':>8s} {'ref_time':>8s} {'score':>8s} {'coverage':>8s}"]
        for sub in ("int", "fp"):
            for g in (g for g in r["groups"] if g["sub"] == sub):
                md.append(f"{g['group']:<19s} {fmt(g['time'])} {g['ref_time']:>8.0f} "
                          f"{fmt(g['score']):>8s} {fmt(g['coverage']):>8s}")
            md.append(f"{name.format(sub) + '/GHz':<19s} {'':>8s} {'':>8s} {fmt(r[f'spec{sub}_per_ghz']):>8s}")
        md += [f"{name.format(''):<19s} {'':>8s} {'':>8s} {fmt(r['spec_per_ghz']):>8s}", "",
               f"{name.format('')}/GHz:    {fmt(r['spec_per_ghz'])}",
               f"{name.format('')}@{args.frequency:2.1f}GHz: {fmt(r['spec_at_freq'])}",
               f"Minimal Coverage: {fmt(r['min_coverage'], '.2f')}/1.00",
               f"Checkpoints     : {len(finished[label])}/{len(points)}", "```", ""]
        if r["missing"]:
            md += [f"missing groups (no finished point or no benchmark): {', '.join(r['missing'])}", ""]
    with open(os.path.join(args.out, "report.md"), "w") as handle:
        handle.write("\n".join(md))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
