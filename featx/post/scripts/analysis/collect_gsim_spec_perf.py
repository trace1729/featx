#!/usr/bin/env python3

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from pathlib import Path


PERF_RE = re.compile(
    r"^\[PERF\s*\]\[time=\s*(\d+)\]\s+(\S+):\s+"
    r"([A-Za-z0-9_@]+)[^,]*,\s+(-?\d+)\s*$"
)

CORE_COUNTERS = (
    "clock_cycle",
    "commitInstr",
    "commit_branch_num",
    "commit_branch_mispredicts",
    "resolve_branch_mispredicts",
    "bpu_train_packets",
    "bpu_train_branches",
    "bpu_train_mispredict_branches",
    "bpu_train_rpt",
    "bpu_train_wpt",
    "bpu_train_pending",
    "bpu_train_duplicate_positions",
    "bpu_train_unclassified_reuse",
)

BRANCH_TYPES = (
    "conditional",
    "direct",
    "indirect",
    "indirect_retcall",
    "call",
    "ret",
)

# These are optional for backward compatibility with logs produced before
# per-branch-type counters were added.  A point remains usable when they are
# absent, but its type-specific MPKI fields are left empty.
BRANCH_TYPE_COUNTERS = tuple(
    name
    for branch_type in BRANCH_TYPES
    for name in (
        f"commit_branch_type_{branch_type}",
        f"commit_branch_mispredicts_type_{branch_type}",
    )
)

TOPDOWN_DIRECT = (
    "NoStall",
    "FlushedInsts",
    "OverrideBubble",
    "FtqUpdateBubble",
    "TAGEMissBubble",
    "SCMissBubble",
    "ITTAGEMissBubble",
    "RASMissBubble",
    "MemVioRedirectBubble",
    "OtherRedirectBubble",
    "FtqFullStall",
    "ICacheMissBubble",
    "ITLBMissBubble",
    "BTBMissBubble",
    "FetchFragBubble",
    "DivStall",
    "IntNotReadyStall",
    "FPNotReadyStall",
    "MemNotReadyStall",
    "IntFlStall",
    "FpFlStall",
    "LoadTLBStall",
    "LoadL1Stall",
    "LoadL2Stall",
    "LoadL3Stall",
    "LoadMemStall",
    "StoreStall",
    "AtomicStall",
    "LoadVioReplayStall",
    "LoadMSHRReplayStall",
    "ControlRecoveryStall",
    "MemVioRecoveryStall",
    "OtherRecoveryStall",
)

TOPDOWN_ALIASES = {
    "IntDqStall": ("IntIQFullStallAlu", "IntIQFullStallBrh", "IntIQFullStallOther"),
    "FpDqStall": ("FpIQFullStall",),
    "LsDqStall": ("LoadIQFullStall", "StoreIQFullStall"),
    "OtherCoreStall": ("BackendOtherCoreStall", "FrontendOtherCoreStall"),
}

POINT_FIELDS = (
    "suite",
    "benchmark",
    "point",
    "weight",
    "point_dir",
    "status",
    "perf_time",
    "perf_block",
    *CORE_COUNTERS,
    "ipc",
    "branch_mpki",
    "resolve_branch_mpki",
    "branch_accuracy",
    "train_mispredict_rate",
    "wpt_fraction",
    "rpt_fraction",
    "wpt_mpki",
    "rpt_mpki",
    "train_branches_per_ki",
    "train_lifecycle_balance",
    *(f"{branch_type}_branch_count" for branch_type in BRANCH_TYPES),
    *(f"{branch_type}_branch_mispredicts" for branch_type in BRANCH_TYPES),
    *(f"{branch_type}_branch_mpki" for branch_type in BRANCH_TYPES),
    *(f"{branch_type}_branch_accuracy" for branch_type in BRANCH_TYPES),
    "topdown_no_stall_raw",
    "topdown_flushed_raw",
    "topdown_unretired_no_stall",
    "topdown_actual_retiring",
    "topdown_actual_flushed",
    "topdown_accounting_fallback",
    "topdown_valid",
    "topdown_missing",
    "topdown_retiring",
    "topdown_frontend_bound",
    "topdown_bad_speculation",
    "topdown_core_bound",
    "topdown_memory_bound",
)


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def fmt(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.12g}"
    return value


def weighted(values):
    pairs = [(value, weight) for value, weight in values if value is not None]
    weight_sum = sum(weight for _, weight in pairs)
    return sum(value * weight for value, weight in pairs) / weight_sum if weight_sum else None


def mean(values):
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def parse_perf_blocks(path):
    blocks = defaultdict(lambda: defaultdict(list))
    with path.open(errors="replace") as log:
        for line in log:
            match = PERF_RE.match(line)
            if match:
                timestamp, hierarchy, name, value = match.groups()
                blocks[int(timestamp)][name].append((hierarchy, int(value)))
    return dict(blocks)


def select_block(blocks, mode):
    timestamps = sorted(blocks)
    if mode == 1:
        index = 0
    elif mode == 2:
        index = 1
    elif mode == 3:
        index = 0
    else:
        raise ValueError(f"unsupported -N value: {mode}")
    required = 2 if mode in (2, 3) else 1
    if len(timestamps) < required:
        raise ValueError(f"need {required} PERF blocks for -N {mode}, found {len(timestamps)}")
    timestamp = timestamps[index]
    return index + 1, timestamp, blocks[timestamp]


def counter(block, name):
    entries = block.get(name, ())
    if not entries:
        return None
    values = {value for _, value in entries}
    if len(entries) != 1 and len(values) != 1:
        locations = ", ".join(f"{hierarchy}={value}" for hierarchy, value in entries)
        raise ValueError(f"ambiguous counter {name}: {locations}")
    return entries[-1][1]


def topdown(block, commit_instr):
    values = {name: counter(block, name) for name in TOPDOWN_DIRECT}
    for logical_name, physical_names in TOPDOWN_ALIASES.items():
        physical_values = [counter(block, name) for name in physical_names]
        values[logical_name] = None if any(value is None for value in physical_values) else sum(physical_values)

    missing = sorted(name for name, value in values.items() if value is None)
    if missing:
        return False, missing, {}, {}

    raw_no_stall = values["NoStall"]
    raw_flushed = values["FlushedInsts"]
    unretired_no_stall = raw_no_stall - commit_instr
    derived_flushed = raw_flushed + unretired_no_stall
    accounting_fallback = derived_flushed < 0
    # Keep the reference TopDown behavior for fusion, move elimination and
    # sampling-boundary cases where NoStall can be slightly below commitInstr.
    actual_flushed = raw_flushed if accounting_fallback else derived_flushed
    actual_retiring = raw_no_stall if accounting_fallback else commit_instr
    frontend = sum(values[name] for name in (
        "OverrideBubble", "FtqFullStall", "ICacheMissBubble", "ITLBMissBubble", "FetchFragBubble"
    ))
    bad_speculation = sum(values[name] for name in (
        "FtqUpdateBubble", "TAGEMissBubble", "SCMissBubble", "ITTAGEMissBubble",
        "RASMissBubble", "BTBMissBubble", "LoadVioReplayStall",
        "ControlRecoveryStall", "MemVioRecoveryStall", "OtherRecoveryStall",
        "MemVioRedirectBubble", "OtherRedirectBubble"
    ))
    core = sum(values[name] for name in (
        "DivStall", "IntNotReadyStall", "FPNotReadyStall", "IntFlStall", "FpFlStall",
        "IntDqStall", "FpDqStall", "LsDqStall", "OtherCoreStall"
    ))
    memory = sum(values[name] for name in (
        "MemNotReadyStall", "LoadTLBStall", "LoadL1Stall", "LoadL2Stall",
        "LoadL3Stall", "LoadMemStall", "StoreStall", "AtomicStall", "LoadMSHRReplayStall"
    ))
    bad_speculation += actual_flushed
    raw = {
        "retiring": actual_retiring,
        "frontend_bound": frontend,
        "bad_speculation": bad_speculation,
        "core_bound": core,
        "memory_bound": memory,
    }
    total = sum(raw.values())
    accounting = {
        "no_stall_raw": raw_no_stall,
        "flushed_raw": raw_flushed,
        "unretired_no_stall": unretired_no_stall,
        "actual_retiring": actual_retiring,
        "actual_flushed": actual_flushed,
        "accounting_fallback": int(accounting_fallback),
    }
    return True, [], {name: ratio(value, total) for name, value in raw.items()}, accounting


def completion_status(point_dir):
    stdout = point_dir / "simulator_out.txt"
    if not stdout.exists():
        return "missing_output"
    text = stdout.read_text(errors="replace")
    if "GOOD TRAP" in text:
        return "good_trap"
    if "EXCEEDING CYCLE/INSTR LIMIT" in text:
        return "limit_reached"
    return "incomplete"


def find_point_dir(root, benchmark, point, weight):
    exact = root / f"{benchmark}_{point}_{weight}"
    if exact.is_dir():
        return exact
    candidates = sorted(root.glob(f"{benchmark}_{point}_*"))
    if len(candidates) == 1:
        return candidates[0]
    return exact


def collect_point(suite, root, benchmark, point, weight_text, mode, include_incomplete):
    weight = float(weight_text)
    point_dir = find_point_dir(root, benchmark, point, weight_text)
    status = completion_status(point_dir)
    row = {field: None for field in POINT_FIELDS}
    row.update({
        "suite": suite,
        "benchmark": benchmark,
        "point": point,
        "weight": weight,
        "point_dir": str(point_dir),
        "status": status,
    })
    if status not in ("good_trap", "limit_reached") and not include_incomplete:
        return row

    stderr = point_dir / "simulator_err.txt"
    if not stderr.exists():
        row["status"] = "missing_log"
        return row
    try:
        block_index, timestamp, block = select_block(parse_perf_blocks(stderr), mode)
        row["perf_time"] = timestamp
        row["perf_block"] = block_index
        for name in CORE_COUNTERS:
            row[name] = counter(block, name)
        missing_core = [name for name in CORE_COUNTERS if row[name] is None]
        if missing_core:
            row["status"] = "missing_counters:" + ";".join(missing_core)
            return row

        cycles = row["clock_cycle"]
        instructions = row["commitInstr"]
        train_total = row["bpu_train_wpt"] + row["bpu_train_rpt"]
        row["ipc"] = ratio(instructions, cycles)
        # TraceRTL suppresses wrong-path execution, so resolve/redirect counts
        # are not directly comparable with execution-driven runs.  Use the
        # mispredicts attributed when the FTQ entry commits as the primary MPKI.
        row["branch_mpki"] = ratio(row["commit_branch_mispredicts"] * 1000, instructions)
        row["resolve_branch_mpki"] = ratio(row["resolve_branch_mispredicts"] * 1000, instructions)
        branch_error_rate = ratio(row["commit_branch_mispredicts"], row["commit_branch_num"])
        row["branch_accuracy"] = None if branch_error_rate is None else 1 - branch_error_rate
        row["train_mispredict_rate"] = ratio(
            row["bpu_train_mispredict_branches"], row["bpu_train_branches"]
        )
        row["wpt_fraction"] = ratio(row["bpu_train_wpt"], train_total)
        row["rpt_fraction"] = ratio(row["bpu_train_rpt"], train_total)
        row["wpt_mpki"] = ratio(row["bpu_train_wpt"] * 1000, instructions)
        row["rpt_mpki"] = ratio(row["bpu_train_rpt"] * 1000, instructions)
        row["train_branches_per_ki"] = ratio(row["bpu_train_branches"] * 1000, instructions)
        row["train_lifecycle_balance"] = (
            row["bpu_train_branches"]
            - row["bpu_train_rpt"]
            - row["bpu_train_wpt"]
            - row["bpu_train_pending"]
            - row["bpu_train_duplicate_positions"]
            - row["bpu_train_unclassified_reuse"]
        )
        for branch_type in BRANCH_TYPES:
            total_name = f"commit_branch_type_{branch_type}"
            mispred_name = f"commit_branch_mispredicts_type_{branch_type}"
            branch_count = counter(block, total_name)
            mispredicts = counter(block, mispred_name)
            row[f"{branch_type}_branch_count"] = branch_count
            row[f"{branch_type}_branch_mispredicts"] = mispredicts
            row[f"{branch_type}_branch_mpki"] = ratio(mispredicts * 1000, instructions) \
                if mispredicts is not None else None
            row[f"{branch_type}_branch_accuracy"] = ratio(
                branch_count - mispredicts, branch_count
            ) if branch_count is not None and mispredicts is not None else None
        valid, missing, ratios, accounting = topdown(block, instructions)
        row["topdown_valid"] = int(valid)
        row["topdown_missing"] = ";".join(missing)
        for name, value in ratios.items():
            row[f"topdown_{name}"] = value
        for name, value in accounting.items():
            row[f"topdown_{name}"] = value
    except ValueError as error:
        row["status"] = f"parse_error:{error}"
    return row


def summarize_benchmark(benchmark, rows):
    usable = [row for row in rows if row["ipc"] is not None]
    selected_weight = sum(row["weight"] for row in rows)
    completed_weight = sum(row["weight"] for row in usable)
    summary = {
        "benchmark": benchmark,
        "selected_points": len(rows),
        "completed_points": len(usable),
        "selected_weight": selected_weight,
        "completed_weight": completed_weight,
        "coverage_fraction": ratio(completed_weight, selected_weight),
    }
    weighted_cpi = weighted((ratio(row["clock_cycle"], row["commitInstr"]), row["weight"]) for row in usable)
    summary["ipc"] = ratio(1, weighted_cpi)
    for name in (
        "clock_cycle", "commitInstr", "branch_mpki", "resolve_branch_mpki", "branch_accuracy",
        "bpu_train_packets", "bpu_train_branches", "bpu_train_mispredict_branches",
        "bpu_train_rpt", "bpu_train_wpt", "train_mispredict_rate", "wpt_fraction",
        "rpt_fraction", "wpt_mpki", "rpt_mpki", "train_branches_per_ki",
        "train_lifecycle_balance", "topdown_retiring", "topdown_frontend_bound",
        "topdown_bad_speculation", "topdown_core_bound", "topdown_memory_bound",
    ):
        summary[name] = weighted((row[name], row["weight"]) for row in usable)
    summary["topdown_valid_points"] = sum(row["topdown_valid"] == 1 for row in usable)
    return summary


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({field: fmt(row.get(field)) for field in fields} for row in rows)


def main():
    parser = argparse.ArgumentParser(description="Collect current XiangShan GSIM SPEC performance counters")
    parser.add_argument("-r", "--root", required=True, type=Path, help="SPEC suite result directory")
    parser.add_argument("--config", required=True, type=Path, help="SPEC point/weight JSON")
    parser.add_argument("--suite", help="suite label; defaults to result directory name")
    parser.add_argument("-N", "--perf-block-mode", type=int, choices=(1, 2, 3), default=2,
                        help="1: first/no-warmup; 2: second/formal; 3: first/warmup")
    parser.add_argument("-o", "--output-dir", type=Path, help="output directory")
    parser.add_argument("--include-incomplete", action="store_true",
                        help="parse the available block even if simulation lacks a completion marker")
    args = parser.parse_args()

    suite = args.suite or args.root.name
    output_dir = args.output_dir or args.root / f"perf-summary-N{args.perf_block_mode}"
    config = json.loads(args.config.read_text())
    point_rows = []
    grouped = defaultdict(list)
    for benchmark, benchmark_config in config.items():
        for point, weight in benchmark_config["points"].items():
            row = collect_point(
                suite, args.root, benchmark, point, str(weight),
                args.perf_block_mode, args.include_incomplete
            )
            point_rows.append(row)
            grouped[benchmark].append(row)

    benchmark_rows = [summarize_benchmark(name, rows) for name, rows in sorted(grouped.items())]
    benchmark_fields = list(benchmark_rows[0]) if benchmark_rows else ["benchmark"]
    write_csv(output_dir / "points.csv", point_rows, POINT_FIELDS)
    write_csv(output_dir / "benchmarks.csv", benchmark_rows, benchmark_fields)

    valid_ipcs = [row["ipc"] for row in benchmark_rows if row["ipc"]]
    completed_points = [row for row in point_rows if row["ipc"] is not None]
    suite_summary = {
        "suite": suite,
        "root": str(args.root),
        "config": str(args.config),
        "perf_block_mode": args.perf_block_mode,
        "configured_points": len(point_rows),
        "completed_points": sum(row["ipc"] is not None for row in point_rows),
        "configured_benchmarks": len(benchmark_rows),
        "fully_completed_benchmarks": sum(row["coverage_fraction"] == 1 for row in benchmark_rows),
        "ipc_geomean_completed_benchmarks": (
            math.exp(sum(math.log(ipc) for ipc in valid_ipcs) / len(valid_ipcs)) if valid_ipcs else None
        ),
        "lifecycle_nonzero_points": sum(
            row["train_lifecycle_balance"] not in (None, 0) for row in point_rows
        ),
        "topdown_invalid_points": sum(
            row["ipc"] is not None and row["topdown_valid"] != 1 for row in point_rows
        ),
    }
    for name in (
        "ipc", "branch_mpki", "resolve_branch_mpki", "branch_accuracy", "train_mispredict_rate", "wpt_fraction",
        "rpt_fraction", "wpt_mpki", "rpt_mpki", "train_branches_per_ki",
        "topdown_retiring", "topdown_frontend_bound", "topdown_bad_speculation",
        "topdown_core_bound", "topdown_memory_bound",
    ):
        suite_summary[f"benchmark_mean_{name}"] = mean(row[name] for row in benchmark_rows)
    for name in (
        "clock_cycle", "commitInstr", "commit_branch_mispredicts", "resolve_branch_mispredicts", "bpu_train_packets",
        "bpu_train_branches", "bpu_train_mispredict_branches", "bpu_train_rpt", "bpu_train_wpt",
    ):
        suite_summary[f"completed_point_total_{name}"] = sum(row[name] for row in completed_points)
    classified_total = (
        suite_summary["completed_point_total_bpu_train_rpt"]
        + suite_summary["completed_point_total_bpu_train_wpt"]
    )
    suite_summary["completed_point_wpt_fraction"] = ratio(
        suite_summary["completed_point_total_bpu_train_wpt"], classified_total
    )
    suite_summary["completed_point_rpt_fraction"] = ratio(
        suite_summary["completed_point_total_bpu_train_rpt"], classified_total
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "suite.json").write_text(json.dumps(suite_summary, indent=2) + "\n")
    print(json.dumps(suite_summary, indent=2))


if __name__ == "__main__":
    main()
