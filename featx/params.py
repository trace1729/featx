"""Headline parameters of a runset, and the recipe keys that differ between runsets.

`Runset.recipes` holds the resolved recipe snapshots taken at submit. This module flattens them
into the few values that say what a runset actually built and ran (the "parameters" card), and
finds the leaf keys on which a node's runsets disagree, so the reason they differ stands out
without reading the YAML.
"""

from __future__ import annotations

import re
from typing import Any

from . import config, points as ptmod, presets, store
from .post import plugins

# Leaves that differ between runsets without saying anything about the experiment.
VOLATILE_KEYS = {"workdir", "runset", "runset_dir", "scripts", "bincache_key", "emu", "task",
                 "created", "stored", "id", "path", "checkpoint_file", "step1_args", "step2_args",
                 "make_args", "args"}
# A path that points into a runset's ${scripts} snapshot (it changes per runset).
SCRIPTS_RE = re.compile(r"/[^\s'\"]*/scripts(?=[/\s'\"]|$)")
RUNSET_RE = re.compile(r"/\d{8}-\d+(?=[/\s'\"]|$)")


def _build_defaults() -> dict:
    return store.read_yaml(config.DEFAULTS_DIR / "recipes" / "build.yaml", {}) or {}


def _flat(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _flat(f"{prefix}.{key}" if prefix else str(key), item, out)
    elif prefix:
        out[prefix] = value


def flatten(recipes: dict) -> dict[str, Any]:
    """{'build.tool': 'gsim', 'run.W': 1000000, 'post.pk.plugin': 'perf-keys', ...}"""
    out: dict[str, Any] = {}
    for kind in ("build", "run", "post"):
        data = recipes.get(kind)
        if isinstance(data, dict):
            _flat(kind, data, out)
    return out


def canonical(key: str, value: Any) -> Any:
    """A value with runset-specific noise (snapshot dirs, bincache keys) removed, for comparing."""
    leaf = key.rsplit(".", 1)[-1]
    if leaf in VOLATILE_KEYS or "scripts" in key or "runset" in key or "bincache" in key:
        return "<volatile>"
    if isinstance(value, str):
        return RUNSET_RE.sub("/<runset>", SCRIPTS_RE.sub("/${scripts}", value))
    if isinstance(value, list):
        return [canonical(key, v) for v in value]
    return value


# Keys resolved from others at submit (the build type from tool+pipeline, a run's commands, trace
# paths and checkpoint catalogue entry from its workload/checkpoint, the point list shown as a
# count): a difference there is already shown by the key it derives from.
DERIVED_PREFIXES = ("build.type", "run.commands.", "run.checkpoint_set.", "run.trace_file", "run.trace_root",
                    "run.points")


def differing(runsets) -> list[str]:
    """The flattened recipe keys whose (volatile-normalized) values differ across runsets.

    A key is compared only among the runsets that ran its stage: a run-only (--bin) runset has no
    build recipe, which is not a difference in every build key. Derived keys are skipped."""
    recs = [rs.recipes if hasattr(rs, "recipes") else rs for rs in runsets]
    flats = [flatten(r or {}) for r in recs]
    if len(flats) < 2:
        return []
    keys = set().union(*(f.keys() for f in flats))
    out = []
    for key in sorted(keys):
        if key.startswith(DERIVED_PREFIXES):
            continue
        stage = key.split(".", 1)[0]
        values = [canonical(key, f.get(key)) for f, r in zip(flats, recs)
                  if isinstance((r or {}).get(stage), dict) and (r or {}).get(stage)]
        if len(values) > 1 and len({repr(v) for v in values}) > 1:
            out.append(key)
    return out


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a legacy/broken snapshot must still render its other keys
        return default


def stage_params(recipes: dict, bin: dict | None = None) -> dict[str, Any]:
    """The headline build/run/post parameters, flat per stage (see `flatten`)."""
    recipes = recipes or {}
    out: dict[str, Any] = {"build": {}, "run": {}, "post": {}}
    build = recipes.get("build") or {}
    if isinstance(build, dict) and build:
        b = out["build"]
        b["tool"] = _safe(lambda: presets.build_tool(build), build.get("tool") or "gsim")
        b["mode"] = build.get("mode") or ("trace" if config.settings().get("trace_enabled") else "exec")
        b["pipeline"] = build.get("pipeline") or "difftest"
        defaults = _build_defaults().get("options") or {}
        for key, value in (build.get("options") or {}).items():
            if defaults.get(key) != value:
                b[f"options.{key}"] = value
        if bin:
            if bin.get("key"):
                b["bincache_key"] = bin["key"]
            if bin.get("cached") is not None:
                b["cache_hit"] = bool(bin.get("cached"))

    run = recipes.get("run") or {}
    if isinstance(run, dict) and run:
        r = out["run"]
        r["mode"] = run.get("mode") or ("trace" if config.settings().get("trace_enabled") else "exec")
        workload = run.get("workload") or _safe(presets.default_workload)
        if workload:
            r["workload"] = workload
        suite = run.get("suite")
        if suite:
            r["suite"] = suite if isinstance(suite, str) else suite.get("config")
        checkpoint = _safe(lambda: presets.run_checkpoint(run))
        if checkpoint:
            r["checkpoint"] = checkpoint
        for key in ("W", "I"):
            if run.get(key) is not None:
                r[key] = run[key]
        # None when the point set can't be resolved here (e.g. a workload missing from this config):
        # a count of 0 would read as "ran nothing".
        names = _safe(lambda: ptmod.resolve(run))
        if names is not None:
            r["points"] = len(names)
        names = names or []
        benches = sorted({_safe(lambda n=n: ptmod.parse(n)["bench"]) for n in names} - {None})
        if benches:
            r["benchmarks"] = benches
        if run.get("coverage"):
            r["coverage"] = run["coverage"]
        if run.get("env"):
            r["env"] = run["env"]
        if run.get("executor"):
            r["executor"] = run["executor"]
        if run.get("cores_per_point"):
            r["cores_per_point"] = run["cores_per_point"]

    post = recipes.get("post") or {}
    for step, spec in ((post.get("steps") or {}) if isinstance(post, dict) else {}).items():
        spec = spec or {}
        prefix = str(step)
        plugin = spec.get("plugin") or ("cmd" if spec.get("cmd") else step)
        out["post"][f"{prefix}.plugin"] = plugin
        base = spec.get("baselines")
        if base:
            out["post"][f"{prefix}.baselines"] = base if isinstance(base, list) else [base]
        with_ = spec.get("with") or {}
        defaults = {}
        if spec.get("plugin"):
            defaults = (_safe(lambda: plugins.get(spec["plugin"]), {}) or {}).get("with") or {}
        for key, value in with_.items():
            if defaults.get(key) != value:
                out["post"][f"{prefix}.with.{key}"] = value
    return out


def overridden_keys(overrides: dict) -> list[str]:
    """Leaf keys of the raw submit overrides, in flatten() notation ('build.options.threads')."""
    flat: dict[str, Any] = {}
    _flat("", overrides or {}, flat)
    return sorted(flat)
