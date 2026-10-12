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


def resolve(spec: dict) -> list[str]:
    names: list[str] = []
    names += [p for p in spec.get("points") or [] if p]
    if spec.get("points_file"):
        names += [l.strip() for l in Path(spec["points_file"]).read_text().splitlines()
                  if l.strip() and not l.startswith("#")]
    suite = spec.get("suite")
    if suite:
        if isinstance(suite, str):
            suite = {"config": suite}
        names += from_suite(suite["config"], suite.get("benchmarks"))
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
