#!/usr/bin/env python3

import argparse
import csv
import importlib.util
import json
import math
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape


METRICS = ("ipc", "branch_mpki", "wpt_fraction", "rpt_fraction")

PERF_RE = re.compile(
    r"^\[PERF\s*\]\[time=\s*(\d+)\]\s+(\S+):\s+"
    r"([A-Za-z0-9_@]+)[^,]*,\s+(-?\d+)\s*$"
)

TOPDOWN_VIEW_MAPS = {
    "coarse": "xs_coarse_rename_map",
    "frontend": "xs_frontend_rename_map",
    "backend": "xs_backend_rename_map",
    "memory": "xs_mem_rename_map",
    "custom": "xs_custom_rename_map",
    "fine": "xs_fine_grain_rename_map",
}

TOPDOWN_ROOT_GROUPS = {
    "retiring_base": ("Base",),
    "frontend_bound": ("Frontend",),
    "bad_speculation": ("BadSpec", "BadSpecInst"),
    "core_bound": ("Core", "FreelistStall", "CoreOther"),
    "memory_bound": ("Load", "Store"),
    "other": ("Misc", "Privileged"),
}

TOPDOWN_POINT_FIELDS = (
    "suite",
    "benchmark",
    "point",
    "weight",
    "ipc_abs_delta",
    "view",
    "category",
    "trace_count",
    "execution_count",
    "trace_topdown_total",
    "execution_topdown_total",
    "trace_fraction",
    "execution_fraction",
    "trace_minus_execution",
    "trace_minus_execution_over_execution_topdown",
)

TOPDOWN_BENCHMARK_FIELDS = (
    "suite",
    "benchmark",
    "paired_points",
    "paired_weight",
    "ipc_abs_delta",
    "view",
    "category",
    "trace_count",
    "execution_count",
    "trace_topdown_total",
    "execution_topdown_total",
    "trace_fraction",
    "execution_fraction",
    "trace_minus_execution",
    "trace_minus_execution_over_execution_topdown",
)

EXCEL_METRICS = (
    ("运行状态", "status", "text"),
    ("IPC", "ipc", "decimal"),
    ("周期数", "clock_cycle", "integer"),
    ("提交指令数", "commitInstr", "integer"),
    ("分支 MPKI", "branch_mpki", "decimal"),
    ("分支预测正确率", "branch_accuracy", "percent"),
    ("提交分支数", "commit_branch_num", "integer"),
    ("分支预测错误数", "resolve_branch_mispredicts", "integer"),
    ("Train Packet 数", "bpu_train_packets", "integer"),
    ("Train 分支数", "bpu_train_branches", "integer"),
    ("Train 中预测错误分支数", "bpu_train_mispredict_branches", "integer"),
    ("Train 中预测错误分支比例", "train_mispredict_rate", "percent"),
    ("WPT 数", "bpu_train_wpt", "integer"),
    ("RPT 数", "bpu_train_rpt", "integer"),
    ("WPT 比例", "wpt_fraction", "percent"),
    ("RPT 比例", "rpt_fraction", "percent"),
    ("WPT MPKI", "wpt_mpki", "decimal"),
    ("RPT MPKI", "rpt_mpki", "decimal"),
    ("Pending Train", "bpu_train_pending", "integer"),
    ("重复位置 Train", "bpu_train_duplicate_positions", "integer"),
    ("未分类复用 Train", "bpu_train_unclassified_reuse", "integer"),
    ("生命周期差值", "train_lifecycle_balance", "integer"),
    ("Topdown Retiring", "topdown_retiring", "percent"),
    ("Topdown Frontend", "topdown_frontend_bound", "percent"),
    ("Topdown Bad Spec.", "topdown_bad_speculation", "percent"),
    ("Topdown Core", "topdown_core_bound", "percent"),
    ("Topdown Memory", "topdown_memory_bound", "percent"),
)

TOPDOWN_EXCEL_METRICS = (
    ("执行驱动计数", "execution_count", "decimal"),
    ("Trace-GSIM 计数", "trace_count", "decimal"),
    ("Trace - 执行", "trace_minus_execution", "decimal"),
    ("执行驱动 TopDown 总量", "execution_topdown_total", "decimal"),
    ("Trace-GSIM TopDown 总量", "trace_topdown_total", "decimal"),
    ("执行驱动占比", "execution_fraction", "percent"),
    ("Trace-GSIM 占比", "trace_fraction", "percent"),
    ("(Trace - 执行) / 执行 TopDown 总量", "trace_minus_execution_over_execution_topdown", "percent"),
)


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def snake_case(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def load_topdown_definitions(path):
    spec = importlib.util.spec_from_file_location("xiangshan_topdown_configs", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load TopDown configuration: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    counter_names = tuple(name for name in module.targets if name != "total_cycles")
    view_maps = {
        view: getattr(module, attribute)
        for view, attribute in TOPDOWN_VIEW_MAPS.items()
    }
    return counter_names, view_maps


def parse_topdown_counters(log_path, timestamp, counter_names):
    values = defaultdict(list)
    with log_path.open(errors="replace") as source:
        for line in source:
            match = PERF_RE.match(line)
            if match is None or int(match.group(1)) != timestamp:
                continue
            _, hierarchy, name, value = match.groups()
            if name in counter_names:
                values[name].append((hierarchy, int(value)))

    counters = {}
    missing = []
    for name in counter_names:
        entries = values.get(name, ())
        if not entries:
            missing.append(name)
            continue
        distinct_values = {value for _, value in entries}
        if len(distinct_values) != 1:
            locations = ", ".join(f"{hierarchy}={value}" for hierarchy, value in entries)
            raise ValueError(f"ambiguous TopDown counter {name}: {locations}")
        counters[name] = entries[-1][1]
    if missing:
        raise ValueError(f"missing TopDown counters: {';'.join(sorted(missing))}")
    return counters


def mapped_topdown_view(counters, mapping, keep_base=True):
    grouped = defaultdict(int)
    for counter, value in counters.items():
        target = mapping.get(counter)
        if target is None:
            continue
        category = target.removeprefix("Merge")
        if category in ("Insts", "Cycles") or (not keep_base and category == "Base"):
            continue
        grouped[snake_case(category)] += value
    return dict(grouped)


def apply_topdown_accounting(counters):
    adjusted = dict(counters)
    raw_no_stall = counters["NoStall"]
    raw_flushed = counters["FlushedInsts"]
    commit_instr = counters["commitInstr"]
    unretired_no_stall = raw_no_stall - commit_instr
    derived_flushed = raw_flushed + unretired_no_stall
    fallback = derived_flushed < 0
    adjusted["FlushedInsts"] = raw_flushed if fallback else derived_flushed
    adjusted["NoStall"] = raw_no_stall if fallback else commit_instr
    accounting = {
        "no_stall_raw": raw_no_stall,
        "flushed_raw": raw_flushed,
        "commit_instr": commit_instr,
        "unretired_no_stall": unretired_no_stall,
        "actual_retiring": adjusted["NoStall"],
        "actual_flushed": adjusted["FlushedInsts"],
        "accounting_fallback": int(fallback),
    }
    return adjusted, accounting


def build_topdown_detail(counters, view_maps):
    counters, accounting = apply_topdown_accounting(counters)
    views = {
        "coarse": mapped_topdown_view(counters, view_maps["coarse"]),
        "leaf": {snake_case(name): value for name, value in counters.items()},
    }
    views["accounting"] = accounting
    for view, mapping in view_maps.items():
        if view == "coarse":
            continue
        views[view] = mapped_topdown_view(counters, mapping, keep_base=False)

    coarse = views["coarse"]
    root = {}
    for root_name, categories in TOPDOWN_ROOT_GROUPS.items():
        root[root_name] = sum(coarse.get(snake_case(category), 0) for category in categories)
    views["root"] = root
    return views, sum(root.values())


def point_topdown_detail(row, counter_names, view_maps, cache):
    point_dir = Path(row["point_dir"])
    if not point_dir.is_absolute():
        point_dir = Path(__file__).resolve().parents[1] / point_dir
    timestamp = row.get("perf_time", "")
    if not timestamp:
        raise ValueError("missing perf_time")
    key = (point_dir, int(timestamp))
    if key not in cache:
        counters = parse_topdown_counters(
            point_dir / "simulator_err.txt", int(timestamp), counter_names
        )
        cache[key] = build_topdown_detail(counters, view_maps)
    return cache[key]


def read_points(path):
    with path.open() as source:
        return {
            (row["benchmark"], row["point"]): row
            for row in csv.DictReader(source)
        }


def valid_points(points):
    return {key: row for key, row in points.items() if row["ipc"]}


def number(row, name):
    return float(row[name])


def optional_number(row, name):
    value = row.get(name, "")
    return float(value) if value != "" else None


def weighted(rows, value):
    total = sum(number(row, "weight") for row in rows)
    return sum(value(row) * number(row, "weight") for row in rows) / total


def benchmark_summary(rows, mode):
    weighted_cpi = weighted(rows, lambda row: number(row, "clock_cycle") / number(row, "commitInstr"))
    result = {"ipc": 1 / weighted_cpi}
    for metric in METRICS[1:]:
        present = [row for row in rows if row.get(metric, "") != ""]
        result[metric] = (
            weighted(present, lambda row, name=metric: number(row, name))
            if present else None
        )
    return result


def ipc_abs_delta(trace_row, execution_row):
    trace_ipc = optional_number(trace_row, "ipc")
    execution_ipc = optional_number(execution_row, "ipc")
    return abs(trace_ipc - execution_ipc) if trace_ipc is not None and execution_ipc is not None else None


def write_csv(path, fields, rows):
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def make_topdown_point_rows(suite, point_details):
    rows = []
    for detail in point_details:
        trace_views = detail["trace_views"]
        execution_views = detail["execution_views"]
        trace_total = detail["trace_total"]
        execution_total = detail["execution_total"]
        for view in sorted(trace_views.keys() | execution_views.keys()):
            trace_categories = trace_views.get(view, {})
            execution_categories = execution_views.get(view, {})
            for category in sorted(trace_categories.keys() | execution_categories.keys()):
                trace_count = trace_categories.get(category, 0)
                execution_count = execution_categories.get(category, 0)
                delta = trace_count - execution_count
                rows.append({
                    "suite": suite,
                    "benchmark": detail["benchmark"],
                    "point": detail["point"],
                    "weight": detail["weight"],
                    "ipc_abs_delta": detail["ipc_abs_delta"],
                    "view": view,
                    "category": category,
                    "trace_count": trace_count,
                    "execution_count": execution_count,
                    "trace_topdown_total": trace_total,
                    "execution_topdown_total": execution_total,
                    "trace_fraction": ratio(trace_count, trace_total),
                    "execution_fraction": ratio(execution_count, execution_total),
                    "trace_minus_execution": delta,
                    "trace_minus_execution_over_execution_topdown": ratio(delta, execution_total),
                })
    return rows


def make_topdown_benchmark_rows(suite, point_details):
    by_benchmark = defaultdict(list)
    for detail in point_details:
        by_benchmark[detail["benchmark"]].append(detail)

    rows = []
    for benchmark, details in by_benchmark.items():
        paired_weight = sum(detail["weight"] for detail in details)
        trace_cpi = weighted(
            details,
            lambda detail: number(detail["trace_row"], "clock_cycle")
            / number(detail["trace_row"], "commitInstr"),
        )
        execution_cpi = weighted(
            details,
            lambda detail: number(detail["execution_row"], "clock_cycle")
            / number(detail["execution_row"], "commitInstr"),
        )
        ipc_delta = abs(1 / trace_cpi - 1 / execution_cpi)
        trace_total = weighted(details, lambda detail: detail["trace_total"])
        execution_total = weighted(details, lambda detail: detail["execution_total"])
        views = set().union(*(detail["trace_views"].keys() | detail["execution_views"].keys() for detail in details))
        for view in sorted(views):
            categories = set().union(*(
                detail["trace_views"].get(view, {}).keys()
                | detail["execution_views"].get(view, {}).keys()
                for detail in details
            ))
            for category in sorted(categories):
                trace_count = weighted(
                    details,
                    lambda detail, current_view=view, current_category=category:
                    detail["trace_views"].get(current_view, {}).get(current_category, 0),
                )
                execution_count = weighted(
                    details,
                    lambda detail, current_view=view, current_category=category:
                    detail["execution_views"].get(current_view, {}).get(current_category, 0),
                )
                delta = trace_count - execution_count
                rows.append({
                    "suite": suite,
                    "benchmark": benchmark,
                    "paired_points": len(details),
                    "paired_weight": paired_weight,
                    "ipc_abs_delta": ipc_delta,
                    "view": view,
                    "category": category,
                    "trace_count": trace_count,
                    "execution_count": execution_count,
                    "trace_topdown_total": trace_total,
                    "execution_topdown_total": execution_total,
                    "trace_fraction": ratio(trace_count, trace_total),
                    "execution_fraction": ratio(execution_count, execution_total),
                    "trace_minus_execution": delta,
                    "trace_minus_execution_over_execution_topdown": ratio(delta, execution_total),
                })
    return rows


def excel_column(index):
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def excel_cell(ref, value, style=0):
    if value is None or value == "":
        return f'<c r="{ref}" s="{style}"/>'
    if isinstance(value, str):
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
    return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'


def excel_value(row, field, kind):
    if row is None:
        return None
    value = row.get(field, "")
    if kind == "text":
        return value
    return float(value) if value != "" else None


def aggregate_excel_rows(points):
    grouped = defaultdict(list)
    for (benchmark, _), row in points.items():
        grouped[benchmark].append(row)
    aggregated = {}
    for benchmark, rows in grouped.items():
        weight_sum = sum(number(row, "weight") for row in rows)
        result = {
            "status": f"{len(rows)} points",
            "weight": weight_sum,
            "point_count": len(rows),
        }
        for _, field, kind in EXCEL_METRICS:
            if kind == "text":
                continue
            present = [row for row in rows if row.get(field, "") != ""]
            if not present:
                result[field] = ""
            elif field == "ipc":
                weighted_cpi = weighted(
                    present,
                    lambda row: number(row, "clock_cycle") / number(row, "commitInstr"),
                )
                result[field] = 1 / weighted_cpi
            else:
                result[field] = weighted(present, lambda row, name=field: number(row, name))
        aggregated[benchmark] = result
    return aggregated


def make_excel_sheet(rows, detail=False):
    fixed_headers = [("Benchmark", 1)]
    fixed_second = ["Benchmark"]
    if detail:
        fixed_headers.extend((("测试点", 1), ("权重", 1)))
        fixed_second.extend(("测试点", "权重"))
    else:
        fixed_headers.extend((("共同测试点数", 1), ("共同权重", 1)))
        fixed_second.extend(("共同测试点数", "共同权重"))
    header1 = fixed_headers + [(label, 2) for label, _, _ in EXCEL_METRICS]
    header2 = fixed_second[:]
    for _ in EXCEL_METRICS:
        header2.extend(("执行驱动", "Trace-GSIM"))

    sheet_rows = []
    first = []
    column = 1
    merges = ["A1:A2", "B1:B2", "C1:C2"]
    for label, span in header1:
        first.append(excel_cell(f"{excel_column(column)}1", label, 1))
        if span == 2:
            merges.append(f"{excel_column(column)}1:{excel_column(column + 1)}1")
        column += span
    sheet_rows.append(f'<row r="1" ht="28" customHeight="1">{"".join(first)}</row>')
    second = [excel_cell(f"{excel_column(index)}2", label, 2) for index, label in enumerate(header2, 1)]
    sheet_rows.append(f'<row r="2" ht="24" customHeight="1">{"".join(second)}</row>')

    styles = {"text": 0, "integer": 3, "decimal": 4, "percent": 5}
    for row_index, (benchmark, point, weight, execution_row, trace_row) in enumerate(rows, 3):
        cells = [excel_cell(f"A{row_index}", benchmark)]
        if detail:
            cells.extend((excel_cell(f"B{row_index}", point), excel_cell(f"C{row_index}", weight, 4)))
        else:
            cells.extend((excel_cell(f"B{row_index}", point, 3), excel_cell(f"C{row_index}", weight, 4)))
        column = 4
        for _, field, kind in EXCEL_METRICS:
            style = styles[kind]
            cells.append(excel_cell(f"{excel_column(column)}{row_index}", excel_value(execution_row, field, kind), style))
            cells.append(excel_cell(f"{excel_column(column + 1)}{row_index}", excel_value(trace_row, field, kind), style))
            column += 2
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    last_column = excel_column(len(header2))
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetViews><sheetView workbookViewId="0"><pane xSplit="3" ySplit="2" topLeftCell="D3" activePane="bottomRight" state="frozen"/></sheetView></sheetViews>
  <cols><col min="1" max="1" width="24" customWidth="1"/><col min="2" max="2" width="18" customWidth="1"/><col min="3" max="3" width="12" customWidth="1"/><col min="4" max="{len(header2)}" width="16" customWidth="1"/></cols>
  <sheetData>{''.join(sheet_rows)}</sheetData>
  <mergeCells count="{len(merges)}">{''.join(f'<mergeCell ref="{item}"/>' for item in merges)}</mergeCells>
  <autoFilter ref="A2:{last_column}{len(rows) + 2}"/>
</worksheet>'''


def make_topdown_excel_sheet(rows, detail=False):
    if detail:
        headers = (
            ("Benchmark", "benchmark", "text"),
            ("测试点", "point", "text"),
            ("权重", "weight", "decimal"),
        )
    else:
        headers = (
            ("Benchmark", "benchmark", "text"),
            ("共同测试点数", "paired_points", "integer"),
            ("共同权重", "paired_weight", "decimal"),
        )
    headers += (
        ("|Trace IPC - 执行 IPC|", "ipc_abs_delta", "decimal"),
        ("视图", "view", "text"),
        ("类别", "category", "text"),
        *TOPDOWN_EXCEL_METRICS,
    )

    styles = {"text": 0, "integer": 3, "decimal": 4, "percent": 5}
    sheet_rows = []
    header_cells = [
        excel_cell(f"{excel_column(index)}1", label, 1)
        for index, (label, _, _) in enumerate(headers, 1)
    ]
    sheet_rows.append(f'<row r="1" ht="28" customHeight="1">{"".join(header_cells)}</row>')
    for row_index, row in enumerate(rows, 2):
        cells = []
        for column, (_, field, kind) in enumerate(headers, 1):
            cells.append(
                excel_cell(
                    f"{excel_column(column)}{row_index}",
                    excel_value(row, field, kind),
                    styles[kind],
                )
            )
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    last_column = excel_column(len(headers))
    frozen_columns = 6 if detail else 5
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetViews><sheetView workbookViewId="0"><pane xSplit="{frozen_columns}" ySplit="1" topLeftCell="{excel_column(frozen_columns + 1)}2" activePane="bottomRight" state="frozen"/></sheetView></sheetViews>
  <cols><col min="1" max="1" width="24" customWidth="1"/><col min="2" max="3" width="18" customWidth="1"/><col min="4" max="{len(headers)}" width="18" customWidth="1"/></cols>
  <sheetData>{''.join(sheet_rows)}</sheetData>
  <autoFilter ref="A1:{last_column}{len(rows) + 1}"/>
</worksheet>'''


def write_excel(path, suite, trace, execution, topdown_benchmarks, topdown_points):
    def point_sort_key(key):
        trace_row = trace.get(key)
        execution_row = execution.get(key)
        delta = ipc_abs_delta(trace_row or {}, execution_row or {})
        return (delta is None, -(delta or 0), key[0], key[1])

    keys = sorted(trace.keys() | execution.keys(), key=point_sort_key)
    detail_rows = []
    for benchmark, point in keys:
        trace_row = trace.get((benchmark, point))
        execution_row = execution.get((benchmark, point))
        weight_row = execution_row or trace_row
        detail_rows.append((benchmark, point, float(weight_row["weight"]), execution_row, trace_row))

    common = {key for key in trace if trace[key].get("ipc") and key in execution and execution[key].get("ipc")}
    trace_common = {key: trace[key] for key in common}
    execution_common = {key: execution[key] for key in common}
    trace_aggregated = aggregate_excel_rows(trace_common)
    execution_aggregated = aggregate_excel_rows(execution_common)
    summary_rows = []
    for benchmark in sorted(trace_aggregated.keys() & execution_aggregated.keys()):
        trace_row = trace_aggregated[benchmark]
        execution_row = execution_aggregated[benchmark]
        summary_rows.append((benchmark, execution_row["point_count"], execution_row["weight"], execution_row, trace_row))
    summary_rows.sort(key=lambda row: (-abs(row[3]["ipc"] - row[4]["ipc"]), row[0]))

    summary_sheet = make_excel_sheet(summary_rows, detail=False)
    detail_sheet = make_excel_sheet(detail_rows, detail=True)
    topdown_benchmark_sheet = make_topdown_excel_sheet(topdown_benchmarks, detail=False)
    topdown_point_sheet = make_topdown_excel_sheet(topdown_points, detail=True)
    workbook = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="加权汇总" sheetId="1" r:id="rId1"/><sheet name="SimPoint明细" sheetId="2" r:id="rId2"/><sheet name="TopDown加权差分" sheetId="3" r:id="rId3"/><sheet name="TopDown明细差分" sheetId="4" r:id="rId4"/></sheets></workbook>'''
    relationships = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/><Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet4.xml"/><Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'''
    root_relationships = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet4.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'''
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="3"><numFmt numFmtId="164" formatCode="0"/><numFmt numFmtId="165" formatCode="0.000000"/><numFmt numFmtId="166" formatCode="0.000%"/></numFmts><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF4472C4"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="2"><border/><border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/></border></borders><cellXfs count="6"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="164" fontId="0" fillId="0" borderId="0" applyNumberFormat="1"/><xf numFmtId="165" fontId="0" fillId="0" borderId="0" applyNumberFormat="1"/><xf numFmtId="166" fontId="0" fillId="0" borderId="0" applyNumberFormat="1"/></cellXfs></styleSheet>'''

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_relationships)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/styles.xml", styles_xml)
        archive.writestr("xl/worksheets/sheet1.xml", summary_sheet)
        archive.writestr("xl/worksheets/sheet2.xml", detail_sheet)
        archive.writestr("xl/worksheets/sheet3.xml", topdown_benchmark_sheet)
        archive.writestr("xl/worksheets/sheet4.xml", topdown_point_sheet)


def main():
    parser = argparse.ArgumentParser(description="Compare paired GSIM SPEC points")
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--execution", required=True, type=Path)
    parser.add_argument("--suite", required=True)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument(
        "--topdown-config",
        type=Path,
        default=Path(__file__).resolve().parent / "top-down" / "configs.py",
        help="XiangShan TopDown mapping configuration",
    )
    args = parser.parse_args()

    trace_all = read_points(args.trace)
    execution_all = read_points(args.execution)
    trace = valid_points(trace_all)
    execution = valid_points(execution_all)
    common = sorted(trace.keys() & execution.keys())
    args.output.mkdir(parents=True, exist_ok=True)
    counter_names, view_maps = load_topdown_definitions(args.topdown_config)
    topdown_cache = {}

    point_rows = []
    point_details = []
    grouped = defaultdict(lambda: {"trace": [], "execution": []})
    for key in common:
        benchmark, point = key
        trace_row = trace[key]
        execution_row = execution[key]
        grouped[benchmark]["trace"].append(trace_row)
        grouped[benchmark]["execution"].append(execution_row)
        row = {"suite": args.suite, "benchmark": benchmark, "point": point, "weight": trace_row["weight"]}
        for metric in METRICS:
            trace_value = optional_number(trace_row, metric)
            execution_value = optional_number(execution_row, metric)
            row[f"trace_{metric}"] = trace_value
            row[f"execution_{metric}"] = execution_value
            row[f"execution_minus_trace_{metric}"] = (
                execution_value - trace_value
                if execution_value is not None and trace_value is not None else None
            )
            row[f"execution_over_trace_{metric}"] = (
                execution_value / trace_value
                if execution_value is not None and trace_value else None
            )
        row["ipc_abs_delta"] = ipc_abs_delta(trace_row, execution_row)

        try:
            trace_views, trace_total = point_topdown_detail(
                trace_row, counter_names, view_maps, topdown_cache
            )
            execution_views, execution_total = point_topdown_detail(
                execution_row, counter_names, view_maps, topdown_cache
            )
        except (OSError, ValueError) as error:
            raise RuntimeError(f"{args.suite} {benchmark}/{point}: {error}") from error

        for category in sorted(trace_views["root"].keys() | execution_views["root"].keys()):
            trace_count = trace_views["root"].get(category, 0)
            execution_count = execution_views["root"].get(category, 0)
            row[f"trace_topdown_{category}"] = trace_count
            row[f"execution_topdown_{category}"] = execution_count
            row[f"trace_minus_execution_topdown_{category}"] = trace_count - execution_count
            row[f"trace_minus_execution_topdown_{category}_over_execution_topdown_total"] = ratio(
                trace_count - execution_count, execution_total
            )
        row["trace_topdown_total"] = trace_total
        row["execution_topdown_total"] = execution_total
        point_rows.append(row)
        point_details.append({
            "benchmark": benchmark,
            "point": point,
            "weight": number(trace_row, "weight"),
            "ipc_abs_delta": row["ipc_abs_delta"],
            "trace_row": trace_row,
            "execution_row": execution_row,
            "trace_views": trace_views,
            "execution_views": execution_views,
            "trace_total": trace_total,
            "execution_total": execution_total,
        })

    benchmark_rows = []
    for benchmark, modes in sorted(grouped.items()):
        trace_summary = benchmark_summary(modes["trace"], "trace")
        execution_summary = benchmark_summary(modes["execution"], "execution")
        row = {
            "suite": args.suite,
            "benchmark": benchmark,
            "paired_points": len(modes["trace"]),
            "paired_weight": sum(number(point, "weight") for point in modes["trace"]),
        }
        for metric in METRICS:
            row[f"trace_{metric}"] = trace_summary[metric]
            row[f"execution_{metric}"] = execution_summary[metric]
            row[f"execution_minus_trace_{metric}"] = (
                execution_summary[metric] - trace_summary[metric]
                if execution_summary[metric] is not None and trace_summary[metric] is not None else None
            )
            row[f"execution_over_trace_{metric}"] = (
                execution_summary[metric] / trace_summary[metric] if trace_summary[metric] else None
            )
        row["ipc_abs_delta"] = abs(row["trace_ipc"] - row["execution_ipc"])
        benchmark_rows.append(row)

    topdown_point_rows = make_topdown_point_rows(args.suite, point_details)
    topdown_benchmark_rows = make_topdown_benchmark_rows(args.suite, point_details)
    root_topdown_by_benchmark = {
        (row["benchmark"], row["category"]): row
        for row in topdown_benchmark_rows
        if row["view"] == "root"
    }
    for row in benchmark_rows:
        for category in TOPDOWN_ROOT_GROUPS:
            detail = root_topdown_by_benchmark[(row["benchmark"], category)]
            row[f"trace_topdown_{category}"] = detail["trace_count"]
            row[f"execution_topdown_{category}"] = detail["execution_count"]
            row[f"trace_minus_execution_topdown_{category}"] = detail["trace_minus_execution"]
            row[f"trace_minus_execution_topdown_{category}_over_execution_topdown_total"] = detail[
                "trace_minus_execution_over_execution_topdown"
            ]
        root_total = root_topdown_by_benchmark[(row["benchmark"], "retiring_base")]
        row["trace_topdown_total"] = root_total["trace_topdown_total"]
        row["execution_topdown_total"] = root_total["execution_topdown_total"]

    point_rows.sort(key=lambda row: (-row["ipc_abs_delta"], row["benchmark"], row["point"]))
    benchmark_rows.sort(key=lambda row: (-row["ipc_abs_delta"], row["benchmark"]))
    topdown_point_rows.sort(
        key=lambda row: (-row["ipc_abs_delta"], row["benchmark"], row["point"], row["view"], row["category"])
    )
    topdown_benchmark_rows.sort(
        key=lambda row: (-row["ipc_abs_delta"], row["benchmark"], row["view"], row["category"])
    )

    def geomean(field):
        values = [row[field] for row in benchmark_rows]
        return math.exp(sum(math.log(value) for value in values) / len(values))

    suite = {
        "suite": args.suite,
        "trace_valid_points": len(trace),
        "execution_valid_points": len(execution),
        "paired_points": len(common),
        "paired_benchmarks": len(benchmark_rows),
        "trace_ipc_geomean": geomean("trace_ipc"),
        "execution_ipc_geomean": geomean("execution_ipc"),
        "point_sort": "abs(trace_ipc - execution_ipc) descending",
        "topdown_config": str(args.topdown_config),
        "topdown_delta_formula": "(trace_count - execution_count) / execution_topdown_total",
        "topdown_total_definition": "sum of XiangShan xs_coarse_rename_map root categories",
    }
    suite["execution_ipc_change"] = suite["execution_ipc_geomean"] / suite["trace_ipc_geomean"] - 1
    for metric in METRICS[1:]:
        trace_values = [row[f"trace_{metric}"] for row in benchmark_rows if row[f"trace_{metric}"] is not None]
        execution_values = [row[f"execution_{metric}"] for row in benchmark_rows if row[f"execution_{metric}"] is not None]
        suite[f"trace_mean_{metric}"] = sum(trace_values) / len(trace_values) if trace_values else None
        suite[f"execution_mean_{metric}"] = sum(execution_values) / len(execution_values) if execution_values else None

    write_csv(args.output / "points.csv", list(point_rows[0]), point_rows)
    write_csv(args.output / "benchmarks.csv", list(benchmark_rows[0]), benchmark_rows)
    write_csv(args.output / "topdown_points.csv", TOPDOWN_POINT_FIELDS, topdown_point_rows)
    write_csv(args.output / "topdown_benchmarks.csv", TOPDOWN_BENCHMARK_FIELDS, topdown_benchmark_rows)
    (args.output / "suite.json").write_text(json.dumps(suite, indent=2) + "\n")
    write_excel(
        args.output / f"{args.suite}-trace-vs-execution.xlsx",
        args.suite,
        trace_all,
        execution_all,
        topdown_benchmark_rows,
        topdown_point_rows,
    )
    print(json.dumps(suite, indent=2))


if __name__ == "__main__":
    main()
