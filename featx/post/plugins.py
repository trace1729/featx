"""Post-processing plugins = YAML manifests (no code change to add a method).

Manifest (featx/post/manifests/<name>.yaml, overridable in $FEATX_ROOT/plugins/<name>.yaml):
  name, description
  with:    default parameters (a step's `with:` overrides them); values may use ${vars}
           and @baseline(..)/@run(..)/@post(..)/@runset(..) references ("self" = this runset)
  after:   default step dependencies (only those present in the recipe are kept)
  cmd:     bash template; must leave summary.json (flat metrics) in ${out} — or name a
           JSON file with `summary_from:` that featx flattens into summary.json.

A recipe step can also skip the manifest and give its own `cmd:` (plus optional `with:`,
`after:`, `summary_from:`), e.g. to call a node helper script: cmd: bash ${scripts}/stats.sh ${run} ${out}

Variables: everything from recipes.base_context plus
  ${out} step output dir, ${run} runset run dir (point dirs), ${points_config} collector JSON
  of this runset's points, ${post.<step>} other step dirs, ${python} interpreter for scripts,
  ${plugin.scripts} featx/post/scripts (the built-in plugins' helpers), and every `with:` key.
  ${scripts} is the runset's snapshot of the node's helper scripts, as in build/run.
  ${workload} / ${workload.label} / ${workload.reference}: the runset run's workload, its
  analysis suite label and its default baseline name (spec06 / exec-0903 without a workload);
  they are substituted before @baseline(..) references are resolved.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

from .. import baselines, config, presets, recipes, store

MANIFEST_DIR = Path(__file__).parent / "manifests"
SCRIPTS_DIR = Path(__file__).parent / "scripts"

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
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "params.yaml").write_text(yaml.safe_dump({"plugin": man.get("name", step_name),
                                                          "with": params}, sort_keys=False))
    env = {"FEATX_ROOT": str(config.root()), "FEATX_TOOLS": config.settings()["analysis_dir"]}
    return "\n".join(lines) + "\n", env


def workload_context(runset_dir: Path) -> dict:
    run = ((store.read_yaml(runset_dir / "experiment.yaml") or {}).get("recipes") or {}).get("run") or {}
    wl = run.get("workload")
    return {"workload": wl or "", "workload.label": presets.label(wl),
            "workload.reference": presets.reference(wl) or ""}


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
