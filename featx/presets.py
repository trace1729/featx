"""Presets: the common knobs as a few recipe keys, lowered to the concrete recipe at submit.

build:  tool    gsim | verilator | script      (legacy `type: gsim-two-step|script` still works)
        mode    trace | exec                   -> TRACERTL_MODE=1|0
        options {chiseldb, chiselmap, constantin, dramsim3, threads, optimize, flags, extra}
                -> the make variables of every make call; the raw gsim.step1_args /
                   gsim.step2_args / verilator.args come after them, so they win.
run:    mode     trace | exec; unset = the build's mode (same submit or the --bin runset)
        workload a name from featx.yaml `workloads` (spec06, spec17, spec26):
                 its `run:` keys become a layer right above the featx defaults (so any recipe
                 key still wins), and with no points / points_file / suite selected the run
                 takes every point of the workload's manifest.
        benchmarks [..] keep only these benchmarks.
        coverage 0<c<=1: per benchmark, the heaviest points until their weights reach c.

The task spec keeps tool/mode/options next to the lowered args (for display); the bincache
key ignores them because the lowered args carry their whole effect. The defaults lower to
exactly the make args featx used before presets, so existing cached builds still hit.
"""

from __future__ import annotations

import copy
import csv
import functools
import shlex
from pathlib import Path

from . import config

MODES = ("trace", "exec")
TOOLS = ("gsim", "verilator", "script")
LEGACY_TYPES = {"gsim-two-step": "gsim"}
TOOL_TYPES = {"gsim": "gsim-two-step", "verilator": "verilator", "script": "script"}
PRESET_KEYS = ("tool", "mode", "options")


class PresetError(ValueError):
    pass


# ================================================================== build
def legacy_layer(kind: str, data: dict) -> dict:
    """A recipe layer's legacy `type:` becomes `tool:` before merging, so a later layer's
    tool (or type) wins as usual."""
    if kind != "build" or not isinstance(data, dict) or "type" not in data:
        return data
    data = dict(data)
    legacy = data.pop("type")
    if legacy is not None and "tool" not in data:
        data["tool"] = LEGACY_TYPES.get(legacy, legacy)
    return data


def build_tool(spec: dict) -> str:
    tool = spec.get("tool") or "gsim"
    if tool not in TOOLS:
        raise PresetError(f"build.tool must be one of {'|'.join(TOOLS)}, got {tool!r}")
    return tool


def build_mode(spec: dict) -> str:
    mode = spec.get("mode") or "trace"
    if mode not in MODES:
        raise PresetError(f"build.mode must be trace|exec, got {mode!r}")
    return mode


def _bit(value) -> int:
    return 1 if value in (True, 1, "1", "true", "yes", "on") else 0


def make_flags(spec: dict, step: str) -> str:
    """Make variables for one make call: step1 (make gsim), step2 (difftest emu) or verilator."""
    o = spec.get("options") or {}
    mode = 1 if build_mode(spec) == "trace" else 0
    threads, opt = o.get("threads", 1), shlex.quote(str(o.get("optimize", "-O3")))
    dram, cst, db = _bit(o.get("dramsim3", True)), _bit(o.get("constantin", False)), _bit(o.get("chiseldb", False))
    if step == "step1":
        parts = [f"EMU_THREADS={threads}", f"TRACERTL_MODE={mode}", f"WITH_DRAMSIM3={dram}",
                 f"WITH_CONSTANTIN={cst}", f"WITH_CHISELDB={db}", f"EMU_OPTIMIZE={opt}"]
    elif step == "step2":
        # difftest always compiles the ChiselDB bridge (its own default is WITH_CHISELDB=1);
        # whether the design has DB tables is decided by step 1.
        parts = ["SIM_TOP=SimTop", "NUM_CORES=1", "RTL_SUFFIX=sv", f"TRACERTL_MODE={mode}",
                 f"WITH_DRAMSIM3={dram}", f"EMU_THREADS={threads}", "WITH_CHISELDB=1",
                 f"WITH_CONSTANTIN={cst}", f"EMU_OPTIMIZE={opt}"]
    elif step == "verilator":
        parts = ["NUM_CORES=1", "RTL_SUFFIX=sv", f"TRACERTL_MODE={mode}", f"WITH_DRAMSIM3={dram}",
                 f"WITH_CONSTANTIN={cst}", f"WITH_CHISELDB={db}", f"EMU_THREADS={threads}",
                 f"EMU_OPTIMIZE={opt}"]
    else:
        raise ValueError(step)
    # Switches that are off by default are only passed when on, so adding one here never
    # changes the args (and the bincache key) of builds that don't use it.
    if _bit(o.get("chiselmap", False)):
        parts.append("WITH_CHISELMAP=1")
    for key, value in (o.get("flags") or {}).items():
        parts.append(f"{key}={shlex.quote(str(value))}")
    if o.get("extra"):
        parts.append(str(o["extra"]))
    return " ".join(parts)


def _join(*args) -> str:
    return " ".join(str(a).strip() for a in args if a is not None and str(a).strip())


def lower_build(spec: dict) -> dict:
    """tool/mode/options -> type + concrete make args. Call once, at submit."""
    spec = copy.deepcopy(spec)
    tool = build_tool(spec)
    spec["tool"], spec["mode"] = tool, build_mode(spec)
    spec["type"] = TOOL_TYPES[tool]
    if tool == "gsim":
        g = spec.setdefault("gsim", {})
        g["step1_args"] = _join(make_flags(spec, "step1"), g.get("step1_args"))
        g["step2_args"] = _join(make_flags(spec, "step2"), g.get("step2_args"))
    elif tool == "verilator":
        v = spec.setdefault("verilator", {})
        v["make_args"] = _join(make_flags(spec, "verilator"), v.get("args"))
    return spec


def key_recipe(spec: dict) -> dict:
    """The recipe part of the bincache key: without the preset keys (the lowered args carry
    them) and without the verilator section unless it is the tool (gsim/script builds keep
    the key they had before verilator support; a verilator build ignores the gsim section)."""
    drop = set(PRESET_KEYS)
    drop.add("gsim" if spec.get("type") == "verilator" else "verilator")
    return {k: v for k, v in spec.items() if k not in drop}


# ================================================================ workload
def workloads() -> dict:
    return config.settings().get("workloads") or {}


def workload(name: str) -> dict:
    found = workloads().get(name)
    if not found:
        raise PresetError(f"unknown workload {name!r}; featx.yaml workloads: {', '.join(sorted(workloads())) or 'none'}")
    return found


def workload_layer(name: str) -> dict:
    return copy.deepcopy(workload(name).get("run") or {})


@functools.lru_cache(maxsize=16)
def _manifest(path: str, mtime: float) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            name = f"{row['benchmark']}_{row['point']}_{row['weight']}"
            rows[name] = row
    return rows


def manifest(name: str | None) -> dict[str, dict]:
    """point name -> manifest row (suite, benchmark, point, weight, checkpoint, trace, ...)."""
    if not name:
        return {}
    path = workload(name).get("manifest")
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        raise PresetError(f"workload {name}: manifest {path} not found")
    return _manifest(str(p), p.stat().st_mtime)


def workload_points(spec: dict) -> list[str]:
    names = list(manifest(spec.get("workload")))
    if not names:
        raise PresetError(f"workload {spec.get('workload')} has no manifest, so it can't list its points; "
                          f"select points / points_file / suite")
    return names


def point_vars(spec: dict, name: str) -> dict:
    row = manifest(spec.get("workload")).get(name) or {}
    return {f"point.{k}": v for k, v in (("trace", row.get("trace")), ("checkpoint", row.get("checkpoint"))) if v}


def label(name: str | None) -> str:
    if not name:
        return "spec06"
    return str(workload(name).get("label") or name)


def insts(name: str | None) -> str:
    """The workload's per-benchmark instruction-count json (spec-score); relative paths are
    under featx/defaults/."""
    path = workload(name or "spec06").get("insts") or ""
    if path and not path.startswith("/"):
        path = str(config.DEFAULTS_DIR / path)
    return path


def reference(name: str | None) -> str | None:
    if not name:
        return "exec-0903"
    return workload(name).get("reference")


# ===================================================================== run
def run_mode(spec: dict, build_spec: dict | None) -> str:
    mode = spec.get("mode")
    built = build_mode(build_spec) if build_spec else None
    if mode is None:
        return built or "trace"
    if mode not in MODES:
        raise PresetError(f"run.mode must be trace|exec, got {mode!r}")
    if built and built != mode:
        raise PresetError(f"run.mode={mode} but the build is mode={built}; set build.mode (the run follows it)")
    return mode


def lower_run(spec: dict, build_spec: dict | None) -> dict:
    spec = copy.deepcopy(spec)
    spec["mode"] = run_mode(spec, build_spec)
    if spec.get("workload"):
        workload(spec["workload"])
    if spec.get("cores_per_point") is None:
        threads = ((build_spec or {}).get("options") or {}).get("threads", 1)
        spec["cores_per_point"] = int(threads or 1)
    if not spec.get("command"):
        commands = spec.get("commands") or {}
        if not commands.get(spec["mode"]):
            raise PresetError(f"run recipe has no commands.{spec['mode']}")
    return spec


def point_command(spec: dict, ctx: dict) -> tuple[str, dict[str, str]]:
    """(command template, inputs that must exist) for one point; ctx has the point vars."""
    from . import recipes
    mode = spec.get("mode") or "trace"
    db = f'--dump-db --db-path="$PWD/chiseldb.sqlite"' if spec.get("dump_db") else ""
    ctx = {**ctx, "db_args": db}
    if mode == "exec":
        ckpt = recipes.substitute(str(spec.get("checkpoint_file") or ""), ctx)
        if not ckpt or "${" in ckpt:
            raise PresetError(f"exec mode: no checkpoint for {ctx.get('point.name')} "
                              f"(set run.checkpoint_file or a workload with checkpoints), got {ckpt!r}")
        ex = spec.get("exec") or {}
        diff = ex.get("diff")
        restorer = ex.get("gcpt_restorer")
        ctx.update({
            "checkpoint_file": ckpt,
            "diff_args": f"--diff {shlex.quote(str(diff))}" if diff else "--no-diff",
            "restorer_args": f"-r {shlex.quote(str(restorer))}" if restorer and ckpt.endswith(".gz") else "",
        })
        inputs = {"checkpoint": ckpt}
        if diff:
            inputs["diff reference"] = str(diff)
    else:
        trace = recipes.substitute(spec["trace_file"], ctx)
        ctx["trace_file"] = trace
        inputs = {"trace file": trace}
    template = spec.get("command") or (spec.get("commands") or {})[mode]
    return recipes.substitute(template, ctx), inputs
