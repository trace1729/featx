"""Named result roots (baselines.yaml) and reference resolution for post steps.

A baseline is {root: <dir of point dirs>, description, binary?, collected?: <collector out
dir with points.csv>}. In post `with:` values (and compare args) these forms resolve:
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

SEED = {
    "exec-0903": {"root": "/nfs/share/gkc/wpe-synthetic_PA/0903/spec06-1.0c/execution",
                  "description": "XiangShan execution-driven reference (synthetic-PA 0903 campaign), 20M+20M"},
    "nowpe-0912": {"root": "/nfs/share/gkc/wpe-NEMU_PA/0912/spec06-1.0c/trace",
                   "description": "TraceRTL, NEMU PA, no wrong-path emulation (0912 unified binary)"},
    "overlay-0928": {"root": "/nfs/share/gkc/wpe-WrongPathEmu-nogenpaddr/0928/spec06-1.0c/trace",
                     "description": "TraceRTL overlay WPE, NEMU PA (0928)",
                     "binary": "/nfs/share/gkc/wpe-WrongPathEmu-nogenpaddr/0928/bin/trace-emu-wpe-gsim"},
    "real-1007": {"root": "/nfs/share/gkc/wpe-WrongPathEmu-nogenpaddr/1007/spec06-1.0c/trace",
                  "description": "TraceRTL real WPE (past-instance wp addresses, 1007)"},
    "memfut-1010": {"root": "/nfs/share/gkc/wpe-WrongPathEmu-nogenpaddr/1010/spec06-1.0c/trace",
                    "description": "TraceRTL real WPE + A' future-instance wp addresses (1010)"},
}


def path() -> Path:
    return config.root() / "baselines.yaml"


def load() -> dict:
    data = store.read_yaml(path())
    if data is None:
        data = dict(SEED)
        if config.root().is_dir():
            store.write_yaml(path(), data)
    return data


def add(name: str, root: str, description: str = "", **extra) -> dict:
    data = load()
    data[name] = {"root": root, "description": description, **{k: v for k, v in extra.items() if v}}
    store.write_yaml(path(), data)
    return data[name]


def remove(name: str) -> None:
    data = load()
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
