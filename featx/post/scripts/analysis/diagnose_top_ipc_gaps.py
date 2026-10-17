#!/usr/bin/env python3
"""Generate per-SimPoint ASCII TopDown diagnoses for TraceRTL versus execution.

The script intentionally consumes the two collector ``points.csv`` files rather
than the already-paired CSV.  Each collector row retains ``point_dir`` and
``perf_time``, which lets this tool read the original PERF block from
``simulator_err.txt`` and report the raw TopDown counters behind the comparison.
"""

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path

from compare_gsim_spec_points import (
    TOPDOWN_ROOT_GROUPS,
    apply_topdown_accounting,
    build_topdown_detail,
    load_topdown_definitions,
    parse_topdown_counters,
    snake_case,
)


ROOT_LABELS = {
    "retiring_base": "Retiring/Base",
    "frontend_bound": "Frontend Bound",
    "bad_speculation": "Bad Speculation",
    "core_bound": "Core Bound",
    "memory_bound": "Memory Bound",
    "other": "Other",
}

NODE_LABELS = {
    "base": "Base / NoStall",
    "frontend": "Frontend",
    "bad_spec": "Bad Spec",
    "bad_spec_inst": "Flushed Instructions",
    "core": "Core",
    "freelist_stall": "Freelist Stall",
    "core_other": "Core Other",
    "load": "Load",
    "store": "Store",
    "misc": "Misc",
    "privileged": "Privileged",
    "exec_stall": "ExecStall",
    "rob_stall": "ROB Stall",
    "fusion": "Fusion",
    "dispatch_policy": "DispatchPolicy",
    "i_q_full_stall": "IQFull",
    "i_qpolicy_stall": "IQEnqPolicy",
    "rab_walk_stall": "Recovery Stall",
    "div_stall": "DivStall",
    "f_p_not_ready_stall": "FP Not Ready Stall",
    "int_not_ready_stall": "Int Not Ready Stall",
    "fp_fl_stall": "FP Freelist Stall",
    "int_fl_stall": "Int Freelist Stall",
    "store_i_q_full_stall": "Store IQFull",
    "control_redirect_stall": "Control Redirect Stall",
    "mem_not_ready_stall": "Memory Not Ready",
    "no_stall": "NoStall",
}

# A coarse node is expanded using the view which represents its direct children.
COARSE_DETAIL_VIEW = {
    "core": "backend",
    "freelist_stall": "backend",
    "core_other": "backend",
    "frontend": "frontend",
    "bad_spec": "fine",
    "bad_spec_inst": "leaf",
    "load": "memory",
    "store": "memory",
    "misc": "leaf",
    "privileged": "backend",
    "base": "leaf",
}

# Expand roots that materially contribute to the Trace/Exec deviation, not only
# the single largest root.  This keeps Bad Speculation visible when Memory Bound
# is the dominant difference.
SIGNIFICANT_ROOT_SHARE = 0.05

BPU_FIELDS = (
    ("退休分支", "commit_branch_num", "integer"),
    ("分支错误 redirect", "resolve_branch_mispredicts", "integer"),
    ("分支 MPKI", "branch_mpki", "decimal"),
    ("Train packet", "bpu_train_packets", "integer"),
    ("Train 分支", "bpu_train_branches", "integer"),
    ("Train 预测错误分支", "bpu_train_mispredict_branches", "integer"),
    ("WPT", "bpu_train_wpt", "integer"),
    ("RPT", "bpu_train_rpt", "integer"),
    ("WPT 比例", "wpt_fraction", "percent"),
    ("RPT 比例", "rpt_fraction", "percent"),
)


def read_points(path):
    with path.open(newline="") as source:
        return {
            (row["benchmark"], row["point"]): row
            for row in csv.DictReader(source)
            if row.get("ipc", "")
        }


def number(row, field):
    value = row.get(field, "")
    return float(value) if value != "" else None


def count(row, field):
    value = number(row, field)
    return int(value) if value is not None else 0


def format_count(value):
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return f"{value / 1_000_000:.3f} M"
    if magnitude >= 1_000:
        return f"{value / 1_000:.3f} K"
    return str(value)


def format_signed_count(value):
    return f"{value:+,}" if abs(value) < 1_000_000 else f"{value / 1_000_000:+.3f} M"


def format_number(value, digits=4):
    return "n/a" if value is None else f"{value:.{digits}f}"


def format_percent(value, digits=2):
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def label(name):
    if name in NODE_LABELS:
        return NODE_LABELS[name]
    if name in ROOT_LABELS:
        return ROOT_LABELS[name]
    return " ".join(part.upper() if part in {"i", "q", "fp", "rob", "btb", "ftq", "ras", "tage"}
                    else part.capitalize() for part in name.split("_"))


def map_category(mapping, counter):
    target = mapping.get(counter)
    if target is None:
        return None
    return snake_case(target.removeprefix("Merge"))


def infer_repo_root(explicit, execution_points):
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    candidates.append(Path.cwd())
    candidates.extend(execution_points.resolve().parents)
    candidates.extend(Path(__file__).resolve().parents)
    for candidate in candidates:
        if (candidate / "scripts" / "top-down" / "configs.py").is_file():
            return candidate
    raise ValueError(
        "unable to infer OpenXiangshan root; pass --repo-root containing scripts/top-down/configs.py"
    )


def resolve_point_dir(row, repo_root):
    point_dir = Path(row["point_dir"])
    return point_dir if point_dir.is_absolute() else repo_root / point_dir


def raw_topdown(row, repo_root, counter_names, view_maps, cache):
    point_dir = resolve_point_dir(row, repo_root)
    timestamp = int(row["perf_time"])
    key = (point_dir, timestamp)
    if key not in cache:
        raw_counters = parse_topdown_counters(
            point_dir / "simulator_err.txt", timestamp, counter_names
        )
        counters, _ = apply_topdown_accounting(raw_counters)
        views, total = build_topdown_detail(raw_counters, view_maps)
        cache[key] = (counters, views, total)
    return cache[key]


def paired_records(trace_points, execution_points):
    records = []
    for benchmark, point in sorted(trace_points.keys() & execution_points.keys()):
        trace = trace_points[(benchmark, point)]
        execution = execution_points[(benchmark, point)]
        trace_ipc = number(trace, "ipc")
        execution_ipc = number(execution, "ipc")
        records.append({
            "benchmark": benchmark,
            "point": point,
            "weight": number(trace, "weight"),
            "trace": trace,
            "execution": execution,
            "trace_ipc": trace_ipc,
            "execution_ipc": execution_ipc,
            "ipc_abs_delta": abs(trace_ipc - execution_ipc),
            "ipc_relative_delta": abs(trace_ipc - execution_ipc) / execution_ipc
            if execution_ipc else None,
        })
    return sorted(records, key=lambda item: (
        -(item["ipc_relative_delta"] or 0), item["benchmark"], item["point"]
    ))


def grouped_detail_children(trace_counters, execution_counters, coarse_name, view_name, view_maps):
    """Group a coarse node's raw counters into its immediate detailed children."""
    result = defaultdict(lambda: [0, 0, []])
    coarse_map = view_maps["coarse"]
    detail_map = view_maps.get(view_name, {})
    all_counters = sorted(set(trace_counters) | set(execution_counters))
    for counter in all_counters:
        if map_category(coarse_map, counter) != coarse_name:
            continue
        detail_name = map_category(detail_map, counter) or snake_case(counter)
        item = result[detail_name]
        item[0] += trace_counters.get(counter, 0)
        item[1] += execution_counters.get(counter, 0)
        item[2].append(counter)
    return {
        name: {"trace": values[0], "execution": values[1], "leaves": values[2]}
        for name, values in result.items()
    }


def node_delta(node):
    return node["trace"] - node["execution"]


def sorted_nodes(nodes):
    return sorted(nodes.items(), key=lambda item: (-abs(node_delta(item[1])), item[0]))


def contribution(trace_count, execution_count):
    """Node Trace/Exec deviation normalized by that node's execution count."""
    if not execution_count:
        return None
    return (trace_count - execution_count) / execution_count


def contribution_to_total(trace_count, execution_count, execution_total):
    """Deviation share used only to select which roots to expand."""
    if not execution_total:
        return None
    return (trace_count - execution_count) / execution_total


def node_text(name, trace_count, execution_count, include_counts=False):
    delta = trace_count - execution_count
    value = f"{label(name)} {format_signed_count(delta)}"
    share = contribution(trace_count, execution_count)
    if share is not None:
        value += f" [{share * 100:+.2f}% 该节点 Exec]"
    if include_counts:
        value += f" (Trace {format_count(trace_count)}, Exec {format_count(execution_count)})"
    return value


def append_tree_line(lines, prefix, is_last, text):
    lines.append(f"{prefix}{'└─' if is_last else '├─'} {text}")
    return prefix + ("   " if is_last else "│  ")


def build_tree(trace_counters, execution_counters, trace_views, execution_views, trace_total, execution_total, view_maps):
    """Return the primary TopDown tree and nodes used for CSV output.

    The tree expands the largest root plus other roots with a large deviation
    share.  For each expanded root, only the largest child path is expanded
    recursively.
    The accompanying CSV retains every node needed for more exhaustive views.
    """
    root_nodes = {
        name: {
            "trace": trace_views["root"].get(name, 0),
            "execution": execution_views["root"].get(name, 0),
        }
        for name in TOPDOWN_ROOT_GROUPS
    }
    nonzero_roots = [(name, node) for name, node in sorted_nodes(root_nodes)
                     if node["trace"] or node["execution"]]
    lines = [
        f"总 TopDown slot：Trace {format_count(trace_total)}，Exec {format_count(execution_total)}，"
        f"Trace - Exec = {format_signed_count(trace_total - execution_total)}",
    ]
    if not nonzero_roots:
        return lines, [], []

    dominant_root = nonzero_roots[0][0]
    expanded_roots = {dominant_root}
    for root_name, root_node in nonzero_roots:
        share = contribution_to_total(
            root_node["trace"], root_node["execution"], execution_total
        )
        if share is not None and abs(share) >= SIGNIFICANT_ROOT_SHARE:
            expanded_roots.add(root_name)
    csv_nodes = []
    paths = []
    for root_name, root_node in nonzero_roots:
        csv_nodes.append(("root", "", root_name, root_node["trace"], root_node["execution"]))
        prefix = append_tree_line(
            lines, "", root_name == nonzero_roots[-1][0],
            node_text(root_name, root_node["trace"], root_node["execution"]),
        )
        if root_name not in expanded_roots:
            continue

        coarse_names = [snake_case(name) for name in TOPDOWN_ROOT_GROUPS[root_name]]
        coarse_nodes = {
            name: {
                "trace": trace_views["coarse"].get(name, 0),
                "execution": execution_views["coarse"].get(name, 0),
            }
            for name in coarse_names
        }
        coarse_nodes = [(name, node) for name, node in sorted_nodes(coarse_nodes)
                        if node["trace"] or node["execution"]]
        if not coarse_nodes:
            continue
        dominant_coarse = coarse_nodes[0][0]
        if root_name == dominant_root:
            paths.append(root_name)
        for coarse_index, (coarse_name, coarse_node) in enumerate(coarse_nodes):
            csv_nodes.append(("coarse", root_name, coarse_name, coarse_node["trace"], coarse_node["execution"]))
            coarse_prefix = append_tree_line(
                lines, prefix, coarse_index == len(coarse_nodes) - 1,
                node_text(coarse_name, coarse_node["trace"], coarse_node["execution"]),
            )
            if coarse_name != dominant_coarse:
                continue

            view_name = COARSE_DETAIL_VIEW.get(coarse_name, "leaf")
            detail_nodes = grouped_detail_children(
                trace_counters, execution_counters, coarse_name, view_name, view_maps
            )
            detail_nodes = [(name, node) for name, node in sorted_nodes(detail_nodes)
                            if node["trace"] or node["execution"]]
            if not detail_nodes:
                continue
            dominant_detail = detail_nodes[0][0]
            if root_name == dominant_root:
                paths.append(coarse_name)
            for detail_index, (detail_name, detail_node) in enumerate(detail_nodes):
                csv_nodes.append(("detail", coarse_name, detail_name,
                                  detail_node["trace"], detail_node["execution"]))
                detail_prefix = append_tree_line(
                    lines, coarse_prefix, detail_index == len(detail_nodes) - 1,
                    node_text(detail_name, detail_node["trace"], detail_node["execution"]),
                )
                if detail_name != dominant_detail:
                    continue
                leaves = {
                    snake_case(counter): {
                        "trace": trace_counters.get(counter, 0),
                        "execution": execution_counters.get(counter, 0),
                    }
                    for counter in detail_node["leaves"]
                }
                leaf_nodes = [(name, node) for name, node in sorted_nodes(leaves)
                              if node["trace"] or node["execution"]]
                if not leaf_nodes:
                    continue
                if root_name == dominant_root:
                    paths.append(detail_name)
                # A one-counter detailed group would otherwise print the same
                # name twice, e.g. Load -> MemoryNotReady -> MemoryNotReady.
                if len(leaf_nodes) == 1 and leaf_nodes[0][0] == detail_name:
                    continue
                if root_name == dominant_root:
                    paths.append(leaf_nodes[0][0])
                for leaf_index, (leaf_name, leaf_node) in enumerate(leaf_nodes):
                    csv_nodes.append(("leaf", detail_name, leaf_name,
                                      leaf_node["trace"], leaf_node["execution"]))
                    append_tree_line(
                        lines, detail_prefix, leaf_index == len(leaf_nodes) - 1,
                        node_text(leaf_name, leaf_node["trace"], leaf_node["execution"]),
                    )
    return lines, csv_nodes, paths


def cpi(row):
    instructions = number(row, "commitInstr")
    cycles = number(row, "clock_cycle")
    return cycles / instructions if instructions else None


def slot_check(total, row):
    cycles = number(row, "clock_cycle")
    if not cycles:
        return None
    return total / cycles


def write_csv(path, fields, rows):
    with path.open("w", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def markdown_table(headers, rows):
    values = ["| " + " | ".join(headers) + " |",
              "| " + " | ".join("---" for _ in headers) + " |"]
    values.extend("| " + " | ".join(row) + " |" for row in rows)
    return values


def report_text(record, trace_views, execution_views, trace_total, execution_total, tree_lines, primary_path):
    trace = record["trace"]
    execution = record["execution"]
    trace_insts = count(trace, "commitInstr")
    execution_insts = count(execution, "commitInstr")
    instruction_delta = trace_insts - execution_insts
    cycle_delta = count(trace, "clock_cycle") - count(execution, "clock_cycle")
    root_nodes = {
        root: (
            trace_views["root"].get(root, 0),
            execution_views["root"].get(root, 0),
        )
        for root in TOPDOWN_ROOT_GROUPS
    }
    dominant_root = max(root_nodes, key=lambda name: abs(root_nodes[name][0] - root_nodes[name][1]))
    bad_trace, bad_execution = root_nodes["bad_speculation"]
    bad_share = contribution(bad_trace, bad_execution)
    path_text = " → ".join(label(item) for item in primary_path) if primary_path else "无有效 TopDown 节点"

    lines = [
        f"# IPC 差异诊断：{record['benchmark']}/{record['point']}",
        "",
        "## 可比性与总体性能",
        "",
    ]
    lines.extend(markdown_table(
        ("指标", "Trace", "执行驱动", "Trace - Exec"),
        (
            ("权重", format_percent(record["weight"]), format_percent(record["weight"]), "单点权重，未参与排序"),
            ("退休指令", f"{trace_insts:,}", f"{execution_insts:,}", f"{instruction_delta:+,}"),
            ("cycle", f"{count(trace, 'clock_cycle'):,}", f"{count(execution, 'clock_cycle'):,}", f"{cycle_delta:+,}"),
            ("IPC", format_number(record["trace_ipc"]), format_number(record["execution_ipc"]),
             format_number(record["trace_ipc"] - record["execution_ipc"])),
            ("IPC 变化百分比", "相对 Exec", "100%", format_percent(
                (record["trace_ipc"] - record["execution_ipc"]) / record["execution_ipc"]
                if record["execution_ipc"] else None, 2)),
            ("Branch MPKI", format_number(number(trace, "branch_mpki"), 6),
             format_number(number(execution, "branch_mpki"), 6),
             format_number(number(trace, "branch_mpki") - number(execution, "branch_mpki"), 6)),
            ("WPT", f"{count(trace, 'bpu_train_wpt'):,}",
             f"{count(execution, 'bpu_train_wpt'):,}",
             f"{count(trace, 'bpu_train_wpt') - count(execution, 'bpu_train_wpt'):+,}"),
            ("RPT", f"{count(trace, 'bpu_train_rpt'):,}",
             f"{count(execution, 'bpu_train_rpt'):,}",
             f"{count(trace, 'bpu_train_rpt') - count(execution, 'bpu_train_rpt'):+,}"),
            ("CPI", format_number(cpi(trace), 6), format_number(cpi(execution), 6),
             format_number(cpi(trace) - cpi(execution), 6)),
            ("TopDown slot/cycle", format_number(slot_check(trace_total, trace), 3),
             format_number(slot_check(execution_total, execution), 3), "应相同，通常为 dispatch 宽度"),
        ),
    ))
    lines.extend((
        "",
        "可比性判定：退休指令差异为 "
        f"{abs(instruction_delta):,}（{abs(instruction_delta) / max(execution_insts, 1) * 100:.4f}%）。"
        "该差异只说明计数规模是否接近，不能单独证明两侧动态指令序列完全相同。",
        "",
        "## TopDown 层级拆解（原始 slot）",
        "",
        "```text",
        *tree_lines,
        "```",
        "",
        "说明：所有偏差均按 `Trace - Exec` 计算：负值表示 Trace 少，正值表示 Trace 多；"
        "两种方向都表示相对执行驱动的偏离，不单独代表性能好坏。"
        "比例为节点偏差除以该节点自身的 `Exec count`，因此符号与节点偏差一致；"
        "当该节点 Exec 为 0 时比例显示为 `n/a`。",
        "",
        "## BPU 交叉检查",
        "",
    ))
    bpu_rows = []
    for display, field, kind in BPU_FIELDS:
        trace_value = number(trace, field)
        execution_value = number(execution, field)
        if kind == "integer":
            trace_text = f"{int(trace_value):,}" if trace_value is not None else "n/a"
            execution_text = f"{int(execution_value):,}" if execution_value is not None else "n/a"
            delta_text = f"{int(trace_value - execution_value):+,}" if trace_value is not None and execution_value is not None else "n/a"
        elif kind == "percent":
            trace_text = format_percent(trace_value, 4)
            execution_text = format_percent(execution_value, 4)
            delta_text = format_percent(trace_value - execution_value, 4)
        else:
            trace_text = format_number(trace_value, 6)
            execution_text = format_number(execution_value, 6)
            delta_text = format_number(trace_value - execution_value, 6)
        bpu_rows.append((display, trace_text, execution_text, delta_text))
    lines.extend(markdown_table(("指标", "Trace", "执行驱动", "Trace - Exec"), bpu_rows))
    lines.extend((
        "",
        "## 自动定位结论",
        "",
        f"- 最大绝对 TopDown 差异位于 `{path_text}`。",
        f"- 根节点 `{label(dominant_root)}` 的 Trace-Exec 差为 "
        f"{format_signed_count(root_nodes[dominant_root][0] - root_nodes[dominant_root][1])} slot。",
        f"- Bad Speculation 相对该节点 Exec 为 {format_percent(bad_share, 2)}；"
        "它只能作为 BPU/redirect 的交叉信号，不能仅凭 WPT、RPT 或 aggregate counter 推导因果。",
        "- 下一步应首先检查主路径叶节点对应功能单元的 issue、busy、完成和依赖链计数；"
        "若要证明错误路径训练影响性能，需在相同执行驱动下进行训练回滚或 commit-only 消融。",
        "",
    ))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Diagnose the N largest TraceRTL versus execution IPC gaps using raw TopDown counters"
    )
    parser.add_argument("--trace", required=True, type=Path, help="Trace collector points.csv")
    parser.add_argument("--execution", required=True, type=Path, help="Execution collector points.csv")
    parser.add_argument("--suite", required=True)
    parser.add_argument("-N", "--top-n", type=int, default=10)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, help="OpenXiangshan root; inferred from execution input when omitted")
    parser.add_argument("--topdown-config", type=Path, help="defaults to <repo-root>/scripts/top-down/configs.py")
    args = parser.parse_args()
    if args.top_n < 1:
        parser.error("--top-n must be at least one")

    repo_root = infer_repo_root(args.repo_root, args.execution)
    topdown_config = args.topdown_config or repo_root / "scripts" / "top-down" / "configs.py"
    counter_names, view_maps = load_topdown_definitions(topdown_config)
    records = paired_records(read_points(args.trace), read_points(args.execution))[:args.top_n]
    if not records:
        raise RuntimeError("no common valid Trace/Exec points")

    output = args.output
    points_output = output / "points"
    points_output.mkdir(parents=True, exist_ok=True)
    cache = {}
    ranking_rows = []
    node_rows = []
    summary = [
        f"# {args.suite}：Top {len(records)} IPC 差异点诊断",
        "",
        "排序键为单点 `|Trace IPC - Exec IPC| / Exec IPC`；SimPoint 权重仅展示，未参与排序。",
        "",
        "| 排名 | 测试点 | 权重 | Trace IPC | Exec IPC | 相对 IPC 差异 | 主 TopDown 路径 |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for rank, record in enumerate(records, 1):
        trace_counters, trace_views, trace_total = raw_topdown(
            record["trace"], repo_root, counter_names, view_maps, cache
        )
        execution_counters, execution_views, execution_total = raw_topdown(
            record["execution"], repo_root, counter_names, view_maps, cache
        )
        tree_lines, nodes, primary_path = build_tree(
            trace_counters, execution_counters, trace_views, execution_views,
            trace_total, execution_total, view_maps,
        )
        report_name = f"{rank:03d}_{record['benchmark']}_{record['point']}.md"
        (points_output / report_name).write_text(
            report_text(record, trace_views, execution_views, trace_total, execution_total,
                        tree_lines, primary_path),
            encoding="utf-8",
        )
        path_text = " -> ".join(primary_path)
        summary.append(
            f"| {rank} | [{record['benchmark']}/{record['point']}](points/{report_name}) | "
            f"{record['weight'] * 100:.4f}% | {record['trace_ipc']:.4f} | "
            f"{record['execution_ipc']:.4f} | {record['ipc_relative_delta'] * 100:.2f}% | `{path_text}` |"
        )
        ranking_rows.append({
            "rank": rank,
            "suite": args.suite,
            "benchmark": record["benchmark"],
            "point": record["point"],
            "weight": record["weight"],
            "trace_ipc": record["trace_ipc"],
            "execution_ipc": record["execution_ipc"],
            "ipc_abs_delta": record["ipc_abs_delta"],
            "ipc_relative_delta": record["ipc_relative_delta"],
            "trace_cycles": count(record["trace"], "clock_cycle"),
            "execution_cycles": count(record["execution"], "clock_cycle"),
            "trace_commit_instructions": count(record["trace"], "commitInstr"),
            "execution_commit_instructions": count(record["execution"], "commitInstr"),
            "trace_topdown_total": trace_total,
            "execution_topdown_total": execution_total,
            "primary_path": path_text,
            "report": f"points/{report_name}",
        })
        for depth, parent, name, trace_count, execution_count in nodes:
            node_rows.append({
                "rank": rank,
                "suite": args.suite,
                "benchmark": record["benchmark"],
                "point": record["point"],
                "depth": depth,
                "parent": parent,
                "node": name,
                "trace_count": trace_count,
                "execution_count": execution_count,
                "trace_minus_execution": trace_count - execution_count,
                "deviation_over_execution_node": contribution(trace_count, execution_count),
                "trace_topdown_total": trace_total,
                "execution_topdown_total": execution_total,
            })

    summary.extend((
        "",
        "每个测试点报告均从两侧 `points.csv` 指向的原始 `simulator_err.txt` PERF block 提取 TopDown counter。",
        "`topdown_nodes.csv` 包含诊断树中所有输出节点的机器可读数据。",
        "",
    ))
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.md").write_text("\n".join(summary), encoding="utf-8")
    write_csv(output / "ranking.csv", tuple(ranking_rows[0]), ranking_rows)
    write_csv(output / "topdown_nodes.csv", tuple(node_rows[0]), node_rows)
    print(f"wrote {len(records)} reports to {output}")


if __name__ == "__main__":
    main()
