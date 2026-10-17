"""Post-processing plugins = YAML manifests (no code change to add a method).

Manifest (featx/post/manifests/<name>.yaml, overridable in $FEATX_ROOT/plugins/<name>.yaml):
  name, description
  with:    default parameters (a step's `with:` overrides them); values may use ${vars}
           and @baseline(..)/@run(..)/@post(..)/@runset(..) references ("self" = this runset)
  after:   default step dependencies (only those present in the recipe are kept)
  cmd:     bash template; must leave summary.json (flat metrics) in ${out} — or name a
           JSON file with `summary_from:` that featx flattens into summary.json.
  requires: featx.yaml keys that must be set (e.g. [paths.perf_trigger]); the plugin is refused
           at submit with a hint while one is unset.

A recipe step can also skip the manifest and give its own `cmd:` (plus optional `with:`,
`after:`, `summary_from:`), e.g. to call a node helper script: cmd: bash ${scripts}/stats.sh ${run} ${out}

Variables: everything from recipes.base_context plus
  ${out} step output dir, ${run} runset run dir (point dirs), ${points_config} collector JSON
  of this runset's points, ${post.<step>} other step dirs, ${python} interpreter for scripts,
  ${plugin.scripts} featx/post/scripts (the built-in plugins' helpers), and every `with:` key.
  ${scripts} is the runset's snapshot of the node's helper scripts, as in build/run.
  ${workload} / ${workload.label} / ${workload.reference} / ${workload.insts}: the runset run's
  workload, its analysis suite label, its default baseline name (spec06 / exec-0903 without a
  workload) and its per-benchmark instruction-count json (spec-score);
  they are substituted before @baseline(..) references are resolved.
  ${checkpoint} / ${checkpoint.insts}: the run's checkpoint set (run.checkpoint, else its
  workload's) and the set's {bench: {insts, points}} json (an implied set without one: the
  workload's insts; an explicit run.checkpoint set without one: empty).
  A step comparing with a baseline / runset of another checkpoint set gets ckpt_mismatch: true
  (and ckpt_mismatch_with) in its summary.json.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import yaml

from .. import baselines, config, presets, recipes, store

MANIFEST_DIR = Path(__file__).parent / "manifests"
SCRIPTS_DIR = Path(__file__).parent / "scripts"

MERGE_PY = r'''
import json, os, sys
path, note = sys.argv[1], json.loads(sys.argv[2])
data = json.load(open(path)) if os.path.isfile(path) else {}
if isinstance(data, dict):
    data.update(note)
    json.dump(data, open(path, "w"), indent=2)
'''

FLATTEN_PY = r'''
import json, sys
src, dst = sys.argv[1], sys.argv[2]
data = json.load(open(src))
flat = {}
def walk(prefix, v):
    if isinstance(v, dict):
        for k, x in v.items():
            walk(f"{prefix}.{k}" if prefix else str(k), x)
    elif isinstance(v, (int, float, str, bool)) or v is None:
        flat[prefix] = v
walk("", data)
json.dump(flat, open(dst, "w"), indent=2)
'''


def manifest_dirs() -> list[Path]:
    return [config.root() / "plugins", MANIFEST_DIR]


def available() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in reversed(manifest_dirs()):
        if d.is_dir():
            for p in sorted(d.glob("*.yaml")):
                data = yaml.safe_load(p.read_text()) or {}
                data["_path"] = str(p)
                out[data.get("name", p.stem)] = data
    return out


def get(name: str) -> dict:
    found = available().get(name)
    if not found:
        raise KeyError(f"unknown post plugin {name!r}; available: {', '.join(sorted(available()))}")
    return found


def manifest(step_name: str, step: dict | None) -> dict:
    """The manifest a recipe step runs: its own inline `cmd:` or the named plugin."""
    step = step or {}
    if step.get("cmd") is not None:
        if step.get("plugin"):
            raise ValueError(f"post step {step_name!r}: set either plugin or cmd, not both")
        return {"name": step_name, "cmd": str(step["cmd"]), "summary_from": step.get("summary_from")}
    return get(step.get("plugin", step_name))


def missing_requirements(man: dict) -> list[str]:
    """The manifest's `requires:` settings keys that are unset."""
    s = config.settings()
    out = []
    for key in man.get("requires") or []:
        node = s
        for part in str(key).split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node in (None, "", [], {}):
            out.append(str(key))
    return out


def requirement_error(step_name: str, man: dict) -> str | None:
    missing = missing_requirements(man)
    if not missing:
        return None
    return (f"post step {step_name!r}: plugin {man.get('name', step_name)} needs {', '.join(missing)} "
            f"in featx.yaml (featx config set {missing[0]}=<path>); see featx plugin show {man.get('name', step_name)}")


def step_after(steps: dict, name: str) -> list[str]:
    st = steps[name] or {}
    deps = st.get("after")
    if deps is None:
        deps = manifest(name, st).get("after") or []
    return [d for d in deps if d in steps and d != name]


def _self_refs(obj, self_ref: str):
    if isinstance(obj, str):
        return (obj.replace("@run(self)", f"@run({self_ref})")
                   .replace("@runset(self)", f"@runset({self_ref})")
                   .replace("@post(self,", f"@post({self_ref},"))
    if isinstance(obj, dict):
        return {k: _self_refs(v, self_ref) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_self_refs(v, self_ref) for v in obj]
    return obj


def _labelled(ref) -> tuple[str, str]:
    """List form of `baselines`: 'exec-0903' -> baseline, 'node/path/20261012-1' -> that
    runset's run dir, '@...' or '/abs' kept; label = baseline name or node:runset."""
    ref = str(ref)
    if ref.startswith(("@", "/")):
        return ref.strip("@/").replace("/", ":").replace("(", ":").replace(")", "")[-40:], ref
    if "/" in ref:
        node, _, rs = ref.rpartition("/")
        return f"{node.rsplit('/', 1)[-1]}:{rs}", f"@run({ref})"
    return ref, f"@baseline({ref})"


def render(tree, task, step_name: str, step: dict, runset_dir: Path, out_dir: Path) -> tuple[str, dict]:
    """Return (bash command, env) for one post step."""
    step = step or {}
    man = manifest(step_name, step)
    why = requirement_error(step_name, man)
    if why:
        raise ValueError(why)
    entry = tree.get(task.node_id)
    ctx = recipes.base_context(tree, entry, runset_dir, task.runset_name)
    ctx.update({
        "out": str(out_dir),
        "run": str(runset_dir / "run"),
        "points_config": str(runset_dir / "run" / "points.json"),
        "python": "python3",
        "plugin.scripts": str(SCRIPTS_DIR),
        "step": step_name,
    })
    for name in (task.spec.get("steps") or {}):
        ctx[f"post.{name}"] = str(runset_dir / "post" / name)
    params = config.deep_merge(man.get("with") or {}, step.get("with") or {})
    if step.get("baselines") is not None:
        params["baselines"] = step["baselines"]
    if isinstance(params.get("baselines"), list):
        params["baselines"] = dict(_labelled(b) for b in params["baselines"])
    params = _self_refs(params, f"{entry.rel}/{task.runset_name}")
    wl_ctx = workload_context(runset_dir)
    ctx.update(wl_ctx)
    params = recipes.substitute(params, wl_ctx)
    if "@baseline()" in repr(params):
        raise ValueError(f"post step {step_name!r}: workload {wl_ctx['workload']!r} has no reference baseline; "
                         f"set the step's with.reference / baselines")
    mismatch = checkpoint_mismatch(params, wl_ctx["checkpoint"], tree)
    params = baselines.resolve_refs(params, tree)
    for _ in range(3):   # params may reference each other and ctx
        params = recipes.substitute(params, {**ctx, **_flat_params(params)})
    ctx.update(_flat_params(params))
    cmd = recipes.substitute(man["cmd"], ctx)
    lines = [f"mkdir -p {shlex.quote(str(out_dir))}", "set -e", cmd.rstrip(), "set +e"]
    if man.get("summary_from"):
        src = recipes.substitute(man["summary_from"], ctx)
        src = src if src.startswith("/") else str(out_dir / src)
        lines.append(f"[ -f {shlex.quote(str(out_dir / 'summary.json'))} ] || python3 - "
                     f"{shlex.quote(src)} {shlex.quote(str(out_dir / 'summary.json'))} <<'FXEOF'\n{FLATTEN_PY}\nFXEOF")
    if mismatch:
        note = json.dumps({"checkpoint": wl_ctx["checkpoint"], "ckpt_mismatch": True, "ckpt_mismatch_with": mismatch})
        lines.append(f"echo {shlex.quote('featx: checkpoint set differs from ' + json.dumps(mismatch))} >&2")
        lines.append(f"python3 -c {shlex.quote(MERGE_PY)} {shlex.quote(str(out_dir / 'summary.json'))} {shlex.quote(note)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "params.yaml").write_text(yaml.safe_dump({"plugin": man.get("name", step_name),
                                                          "with": params}, sort_keys=False))
    env = {"FEATX_ROOT": str(config.root()), "FEATX_TOOLS": str(config.analysis_dir())}
    return "\n".join(lines) + "\n", env


def _run_recipe(runset_dir: Path) -> dict:
    return ((store.read_yaml(runset_dir / "experiment.yaml") or {}).get("recipes") or {}).get("run") or {}


def workload_context(runset_dir: Path) -> dict:
    from .. import ckpt
    run = _run_recipe(runset_dir)
    wl = presets.context_workload(run)
    insts = presets.insts(wl)
    ck = presets.run_checkpoint(run) or ""
    info = run.get("checkpoint_set") or {}
    ck_insts = ckpt.insts_path(ck, info.get("path"))
    if not ck_insts and not (info.get("path") and not info.get("implied")):
        ck_insts = insts          # an explicit set's points don't take another set's insts
    return {"workload": wl or "", "workload.label": presets.label(wl),
            "workload.reference": presets.reference(wl) or "", "workload.insts": insts,
            "checkpoint": ck, "checkpoint.insts": ck_insts}


def checkpoint_mismatch(params, mine: str, tree) -> dict:
    """label/ref -> checkpoint set, for the @baseline / @run / @runset references in `params`
    whose set is known and differs from this run's."""
    if not mine:
        return {}
    out = {}
    for m in baselines.REF_RE.finditer(repr(params)):
        kind, arg = m.group(1), m.group(2).strip()
        other = None
        if kind == "baseline":
            other = (baselines.load().get(arg) or {}).get("checkpoint")
        elif kind in ("run", "runset") and arg != "self":
            try:
                entry, rs = tree.find_runset(arg)
                other = presets.run_checkpoint(rs.recipes.get("run") or {})
            except Exception:  # noqa: BLE001 - resolve_refs reports bad refs
                other = None
        if other and other != mine:
            out[arg] = other
    return out


def _flat_params(params: dict) -> dict:
    out = {}
    for k, v in params.items():
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            out[k] = v
        elif isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
            out[k] = " ".join(shlex.quote(str(x)) for x in v)
        elif isinstance(v, dict) and all(isinstance(x, (str, int, float)) for x in v.values()):
            out[k] = " ".join(shlex.quote(f"{kk}={vv}") for kk, vv in v.items())
        elif isinstance(v, bool):
            out[k] = "1" if v else ""
    return out
