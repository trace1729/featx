"""Named result roots (baselines.yaml) and reference resolution for post steps.

A baseline is {root: <dir of point dirs>, description, binary?, collected?: <collector out
dir with points.csv>, checkpoint?: the checkpoint set its points come from}. In post `with:` values (and compare args) these forms resolve:
  @baseline(NAME)            -> baseline root (point dirs)
  @baseline(NAME).collected  -> its collected dir (if registered)
  @runset(REF)               -> runset dir      REF = <node path or id>/<YYYYMMDD-n>
  @run(REF)                  -> runset run/ dir (point dirs)
  @post(REF,STEP)            -> runset post/<STEP> dir
"""

from __future__ import annotations

import re
from pathlib import Path

from . import config, store

REF_RE = re.compile(r"@(baseline|runset|run|post)\(([^()]*)\)(\.collected)?")

def path() -> Path:
    return config.root() / "baselines.yaml"


def _own() -> dict:
    return store.read_yaml(path()) or {}


def load() -> dict:
    """featx.yaml `baselines` (the site profile's shared roots + the user layer) overlaid per
    entry by $FEATX_ROOT/baselines.yaml (`featx baseline add`); a null entry there hides one."""
    shared = config.settings().get("baselines") or {}
    own = _own()
    data = {}
    for name in [*shared, *(n for n in own if n not in shared)]:
        if name in own and own[name] is None:
            continue
        entry = {**(shared.get(name) or {}), **(own.get(name) or {})}
        if entry.get("root"):
            data[name] = entry
    return data


def add(name: str, root: str, description: str = "", **extra) -> dict:
    data = _own()
    data[name] = {"root": root, "description": description, **{k: v for k, v in extra.items() if v}}
    store.write_yaml(path(), data)
    return load()[name]


def remove(name: str) -> None:
    data = _own()
    if name in (config.settings().get("baselines") or {}):
        data[name] = None
    else:
        data.pop(name, None)
    store.write_yaml(path(), data)


def resolve_refs(obj, tree=None):
    """Expand @baseline/@runset/@run/@post references in strings (recursively)."""
    if isinstance(obj, dict):
        return {k: resolve_refs(v, tree) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_refs(v, tree) for v in obj]
    if not isinstance(obj, str) or "@" not in obj:
        return obj

    def repl(m: re.Match) -> str:
        kind, arg, collected = m.group(1), m.group(2).strip(), m.group(3)
        if kind == "baseline":
            b = load().get(arg)
            if not b:
                raise KeyError(f"unknown baseline {arg!r}")
            if collected:
                if not b.get("collected"):
                    raise KeyError(f"baseline {arg!r} has no collected dir")
                return b["collected"]
            return b["root"]
        from .tree import Tree
        t = tree or Tree()
        if kind == "post":
            ref, _, step = arg.rpartition(",")
            entry, rs = t.find_runset(ref.strip())
            return str(entry.dir / rs.name / "post" / step.strip())
        entry, rs = t.find_runset(arg)
        rdir = entry.dir / rs.name
        return str(rdir if kind == "runset" else rdir / "run")

    return REF_RE.sub(repl, obj)
