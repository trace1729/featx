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


def resolve(spec: dict, strict: bool = False) -> list[str]:
    """points ∪ points_file ∪ suite; none of them + a workload = all the workload's points.
    `benchmarks` then keeps only those benchmarks. With a workload every point must be in
    its manifest. strict (submit validation): `benchmarks` may not drop a point named
    explicitly in `points`, nor leave nothing of a non-empty selection."""
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
    if wl and not names:
        names = presets.workload_points(spec)
    benches = spec.get("benchmarks") or []
    if benches:
        selected = names
        names = [n for n in names if parse(n)["bench"] in benches]
        if strict:
            dropped = [n for n in explicit if parse(n)["bench"] not in benches]
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
    if wl:
        known = presets.manifest(wl)
        missing = [n for n in names if known and n not in known]
        if missing:
            raise presets.PresetError(
                f"{len(missing)} point(s) are not in workload {wl}: {', '.join(missing[:5])}"
                f"{' ...' if len(missing) > 5 else ''} (an inherited suite/points? set it to null)")
    return list(dict.fromkeys(names))


def write_config(names: list[str], path: Path) -> Path:
    """Spec-config JSON (collect_gsim_spec_perf.py --config format) for exactly these points."""
    data: dict = {}
    for name in names:
        p = parse(name)
        data.setdefault(p["bench"], {"insts": "0", "points": {}})["points"][p["cp"]] = p["weight"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path
