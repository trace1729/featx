"""SPEC checkpoint points: names are <bench>_<cp>_<weight> (bench may contain '_'),
the same as perf_trigger / collect_gsim_spec_perf.py result dirs."""

from __future__ import annotations

import json
from pathlib import Path


def parse(name: str) -> dict:
    rest, weight = name.rsplit("_", 1)
    bench, cp = rest.rsplit("_", 1)
    return {"name": name, "bench": bench, "cp": cp, "weight": weight}


def from_suite(config_json: str, benchmarks: list[str] | None = None) -> list[str]:
    data = json.loads(Path(config_json).read_text())
    out = []
    for bench, info in data.items():
        if benchmarks and bench not in benchmarks:
            continue
        for cp, weight in (info.get("points") or {}).items():
            out.append(f"{bench}_{cp}_{weight}")
    return out


def by_coverage(names: list[str], coverage: float) -> list[str]:
    """Per benchmark, the heaviest points (ties: lower checkpoint first) until their weights
    sum to `coverage` — the rule behind the <suite>-0.3c.json configs."""
    groups: dict[str, list[dict]] = {}
    for name in names:
        p = parse(name)
        groups.setdefault(p["bench"], []).append(p)
    keep: set[str] = set()
    for pts in groups.values():
        total = 0.0
        for p in sorted(pts, key=lambda p: (-float(p["weight"]), int(p["cp"]) if p["cp"].isdigit() else 0,
                                            p["cp"])):
            if total >= coverage - 1e-9:
                break
            keep.add(p["name"])
            total += float(p["weight"])
    return [n for n in names if n in keep]


def resolve(spec: dict, strict: bool = False) -> list[str]:
    """points ∪ points_file ∪ suite; none of them + an exec run.checkpoint set = all the set's
    points, + a workload = all the workload's points.
    `benchmarks` then keeps only those benchmarks, and `coverage` (0 < c ≤ 1) only the
    heaviest points of each benchmark up to that weight. With a workload every point must
    be in its manifest (with a checkpoint set, in the set). strict (submit validation): `benchmarks` may not drop a point named
    explicitly in `points`, nor leave nothing of a non-empty selection; `coverage` may not
    be combined with explicit points."""
    from . import presets
    names: list[str] = []
    explicit = [p for p in spec.get("points") or [] if p]
    names += explicit
    if spec.get("points_file"):
        names += [l.strip() for l in Path(spec["points_file"]).read_text().splitlines()
                  if l.strip() and not l.startswith("#")]
    suite = spec.get("suite")
    if suite:
        if isinstance(suite, str):
            suite = {"config": suite}
        names += from_suite(suite["config"], suite.get("benchmarks"))
    wl = spec.get("workload")
    ck = presets.checkpoint_index(spec)
    if ck is not None and not names:
        names = list(ck["points"])
    elif wl and not names:
        names = presets.workload_points(spec)
    benches = spec.get("benchmarks") or []
    if benches:
        selected = names
        names = [n for n in names if bench_selected(parse(n)["bench"], benches)]
        if strict:
            dropped = [n for n in explicit if not bench_selected(parse(n)["bench"], benches)]
            if dropped:
                raise presets.PresetError(
                    f"benchmarks={benches} drops {len(dropped)} point(s) given by points/--point: "
                    f"{', '.join(dropped[:5])}{' ...' if len(dropped) > 5 else ''} (an inherited "
                    f"benchmarks filter? set it to null: --set run.benchmarks=null)")
            if selected and not names:
                known = sorted({parse(n)["bench"] for n in selected})
                raise presets.PresetError(
                    f"benchmarks={benches} matches none of the {len(selected)} selected points; "
                    f"their benchmarks are: {', '.join(known[:12])}{' ...' if len(known) > 12 else ''}")
    coverage = spec.get("coverage")
    if coverage is not None:
        try:
            coverage = float(coverage)
        except (TypeError, ValueError):
            raise presets.PresetError(f"coverage must be a number in (0, 1], got {coverage!r}") from None
        if not 0 < coverage <= 1:
            raise presets.PresetError(f"coverage must be in (0, 1], got {coverage}")
        if strict and explicit:
            raise presets.PresetError(
                "coverage selects points by weight and can't be combined with points/--point "
                "(an inherited coverage? set it to null: --set run.coverage=null)")
        if coverage < 1:
            names = by_coverage(names, coverage)
    if ck is not None or wl:
        known = ck["points"] if ck is not None else presets.manifest(wl)
        where = f"checkpoint set {ck['name']}" if ck is not None else f"workload {wl}"
        missing = [n for n in names if known and n not in known]
        if missing:
            raise presets.PresetError(
                f"{len(missing)} point(s) are not in {where}: {', '.join(missing[:5])}"
                f"{' ...' if len(missing) > 5 else ''} (an inherited suite/points? set it to null)")
    return list(dict.fromkeys(names))


def bench_selected(bench: str, wanted: list[str]) -> bool:
    """`benchmarks` entries match a benchmark exactly or as a prefix up to '_' / '.'
    ("500.perlbench" selects 500.perlbench_rate_refrate_00_checkspam...)."""
    return any(bench == w or bench.startswith((f"{w}_", f"{w}.")) for w in wanted)


def write_config(names: list[str], path: Path, insts: dict | None = None) -> Path:
    """Spec-config JSON (collect_gsim_spec_perf.py --config format) for exactly these points;
    `insts` = per-benchmark instruction counts (a checkpoint set's), else "0"."""
    data: dict = {}
    for name in names:
        p = parse(name)
        data.setdefault(p["bench"], {"insts": (insts or {}).get(p["bench"], "0"), "points": {}})["points"][p["cp"]] = p["weight"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path
