"""Recipe inheritance and ${var} substitution.

Effective recipe for (node, kind) = deep merge, later wins:
  package defaults/recipes/<kind>.yaml -> $FEATX_ROOT/recipes/<kind>.yaml
  -> each ancestor's recipes/<kind>.yaml (root first) -> the node's own.
Dicts merge recursively, lists replace, `key: null` deletes.

Substitution only touches ${name} where name is known (dotted names allowed), so shell
syntax like ${HOME} or ${x%%=*} passes through untouched.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from . import config, store
from .tree import Entry, Tree, TreeError

RECIPE_KINDS = ("build", "run", "post")
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")


def _defaults(kind: str) -> dict:
    return store.read_yaml(config.DEFAULTS_DIR / "recipes" / f"{kind}.yaml", {}) or {}


def layers(tree: Tree, entry: Entry, kind: str) -> list[tuple[str, dict]]:
    if kind not in RECIPE_KINDS:
        raise TreeError(f"recipe kind must be one of {RECIPE_KINDS}")
    out = [("featx-defaults", _defaults(kind))]
    user = store.read_yaml(config.root() / "recipes" / f"{kind}.yaml")
    if user:
        out.append(("root", user))
    for e in tree.ancestors(entry) + [entry]:
        data = store.read_yaml(e.dir / "recipes" / f"{kind}.yaml")
        if data:
            out.append((e.rel, data))
    return out


def effective(tree: Tree, entry: Entry, kind: str, overrides: dict | None = None) -> dict:
    merged: dict = {}
    for _src, data in layers(tree, entry, kind):
        merged = config.deep_merge(merged, data)
    if overrides:
        merged = config.deep_merge(merged, overrides)
    return merged


def own(entry: Entry, kind: str) -> dict:
    return store.read_yaml(entry.dir / "recipes" / f"{kind}.yaml", {}) or {}


def set_own(entry: Entry, kind: str, data: dict | str) -> dict:
    if isinstance(data, str):
        data = yaml.safe_load(data) or {}
    if not isinstance(data, dict):
        raise TreeError("recipe must be a YAML mapping")
    store.write_yaml(entry.dir / "recipes" / f"{kind}.yaml", data)
    return data


def patch_own(entry: Entry, kind: str, patch: dict) -> dict:
    return set_own(entry, kind, config.deep_merge(own(entry, kind), patch))


def substitute(obj, ctx: dict, *, rounds: int = 6):
    """Replace known ${vars} in every string of obj (recursively)."""
    if isinstance(obj, str):
        text = obj
        for _ in range(rounds):
            new = VAR_RE.sub(lambda m: str(ctx[m.group(1)]) if m.group(1) in ctx else m.group(0), text)
            if new == text:
                break
            text = new
        return text
    if isinstance(obj, dict):
        return {k: substitute(v, ctx, rounds=rounds) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, ctx, rounds=rounds) for v in obj]
    return obj


def scalar_vars(recipe: dict, prefix: str = "") -> dict:
    """Top-level scalars of a recipe become ${key} vars (e.g. ${W}, ${trace_root})."""
    out = {}
    for key, value in recipe.items():
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            out[prefix + key] = value
    return out


def base_context(tree: Tree, entry: Entry, runset_dir: Path | None = None, runset_name: str | None = None) -> dict:
    s = config.settings()
    ctx = {
        "node.path": entry.rel,
        "node.name": entry.node.name,
        "node.id": entry.node.id,
        "node.dir": str(entry.dir),
        "featx.root": str(config.root()),
        "featx.home": str(config.REPO_DIR),
        "util": s["util_dir"],
        "tools": s["analysis_dir"],
        "date": store.today(),
    }
    if entry.node.git:
        ctx["worktree"] = entry.node.git.worktree
        ctx["repo"] = entry.node.git.repo
        ctx["branch"] = entry.node.git.branch
        for name, sub in entry.node.git.submodule_worktrees.items():
            ctx[f"sub.{name}"] = sub.path
    if runset_dir is not None:
        ctx["runset.dir"] = str(runset_dir)
        ctx["runset.name"] = runset_name or runset_dir.name
        ctx["bin"] = str(runset_dir / "bin" / "emu")
    return ctx


def explain(tree: Tree, entry: Entry, kind: str) -> str:
    """Effective recipe plus which layer set each top-level key."""
    origin: dict[str, str] = {}
    for src, data in layers(tree, entry, kind):
        for key in data:
            origin[key] = src
    eff = effective(tree, entry, kind)
    lines = [yaml.safe_dump(eff, sort_keys=False, allow_unicode=True).rstrip(), "", "# key origins:"]
    lines += [f"#   {k}: {v}" for k, v in origin.items() if k in eff]
    return "\n".join(lines)
