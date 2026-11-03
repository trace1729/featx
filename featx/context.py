"""Node context bundle: everything an LLM (or human) needs to continue work on a node."""

from __future__ import annotations

from pathlib import Path

from . import params as params_mod
from . import coderec, recipes, store, taskstore, worktree
from .tree import Entry, Tree


def node_summary(entry: Entry) -> dict:
    n = entry.node
    return {"id": n.id, "path": entry.rel, "kind": n.kind, "hypothesis": n.hypothesis,
            "verdict": n.verdict, "verdict_note": n.verdict_note, "status": n.status,
            "evidence": n.evidence, "tags": n.tags, "comment": n.comment}


def code_summary(entry: Entry, rs) -> dict:
    """What the code column needs without the code API: the HEAD the build recorded and how much
    uncommitted code its saved patches hold, else the HEAD (and src-patches) a runset from before
    code records kept at submit (`legacy`); a run-only (--bin) runset names the runset that built
    its binary (bincache manifest), like the code API does."""
    prov = rs.provenance or {}
    bin = rs.bin or {}
    source = None
    if not (rs.recipes or {}).get("build") and bin.get("emu"):
        man = store.read_json(Path(bin["emu"]).parent / "manifest.json", {}) or {}
        if man.get("node") and man.get("runset"):
            source = f"{man['node']}/{man['runset']}"
            if source == f"{entry.rel}/{rs.name}":
                source = None
    code = prov.get("code") or {}
    legacy = not (code.get("super") or {}).get("head")
    if not legacy:
        repos = {"": code["super"], **(code.get("submodules") or {})}
        head = code["super"]["head"]
        stats = {n: {k: r.get(k, 0) for k in ("files", "added", "deleted")} for n, r in repos.items() if r.get("dirty")}
        omitted = sum(len(r.get("omitted") or []) for r in repos.values())
    else:
        sup = prov.get("super") or {}
        head = sup.get("head") or prov.get("head")
        pdir = entry.dir / rs.name / "src-patches"
        stats = {}
        for path in sorted(pdir.glob("*.patch")) if pdir.is_dir() else []:
            name = "" if path.name == "super.patch" else path.stem.replace("__", "/")
            stats[name] = coderec.patch_text_stat(path.read_bytes())
        omitted = 0
    dirty = bool(stats) or (legacy and bool((prov.get("super") or {}).get("dirty", prov.get("dirty"))))
    uncommitted = None
    if stats:
        uncommitted = {"repos": sorted(stats), **{k: sum(s[k] for s in stats.values()) for k in ("files", "added", "deleted")}}
    return {"head": head, "branch": (code.get("super") or prov.get("super") or {}).get("branch"),
            "dirty": dirty, "uncommitted": uncommitted, "omitted": omitted, "legacy": legacy and bool(head),
            "from": source, "imported": bool(bin.get("imported")),
            "bin_key": bin.get("key"), "bin_emu": bin.get("emu")}


def runset_summary(entry: Entry, rs, tasks: dict) -> dict:
    rdir = entry.dir / rs.name
    out = {"ref": f"{entry.rel}/{rs.name}", "id": rs.id, "purpose": rs.purpose, "created": rs.created,
           "sealed": rs.sealed, "derived_from": rs.derived_from, "dir": str(rdir), "tasks": [],
           "archived": rs.archived, "archived_at": rs.archived_at, "archive_note": rs.archive_note,
           "overrides": rs.overrides, "params": params_mod.stage_params(rs.recipes, rs.bin),
           "code": code_summary(entry, rs)}
    for tid in rs.tasks:
        t = tasks.get(tid)
        if t:
            out["tasks"].append({"id": t.id, "kind": t.kind, "name": t.name, "state": t.state,
                                 "priority": t.priority, "counts": t.counts(), "failure": (t.failure or {}).get("signature")})
            if t.kind == "run" and t.jobs and isinstance(out["params"].get("run"), dict):
                # the points that were actually submitted; re-resolving the recipe follows today's manifests
                out["params"]["run"]["points"] = len(t.jobs)
    posts = {}
    pdir = rdir / "post"
    if pdir.is_dir():
        for step in sorted(pdir.iterdir()):
            summary = store.read_json(step / "summary.json")
            if summary is not None:
                posts[step.name] = summary
    out["post"] = posts
    plog = rdir / "problem-analysis.log"
    if plog.is_file():
        text = plog.read_text(errors="replace")
        out["problem_log_tail"] = text[-3000:]
    return out


def build(tree: Tree, entry: Entry, *, max_runsets: int = 3, include_archived: bool = False,
          archived_only: bool = False) -> dict:
    if max_runsets < 0:
        raise ValueError("max_runsets must be non-negative")
    tasks = taskstore.load_all()
    scripts_dir = entry.dir / "scripts"
    scripts = sorted(p.name for p in scripts_dir.iterdir()) if scripts_dir.is_dir() else []
    all_runsets = tree.runsets(entry)
    archived = [rs for rs in all_runsets if rs.archived]
    runsets = [rs for rs in all_runsets if rs.archived] if archived_only else (
        all_runsets if include_archived else [rs for rs in all_runsets if not rs.archived])
    shown = runsets[-max_runsets:] if max_runsets else []
    differing = params_mod.differing(shown)
    summaries = []
    for rs in shown:
        summary = runset_summary(entry, rs, tasks)
        flat = params_mod.flatten(rs.recipes or {})
        # this runset's value of every differing key (absent: its stage has no such key)
        summary["differing_values"] = {k: flat[k] for k in differing if k in flat}
        summaries.append(summary)
    return {
        "node": node_summary(entry),
        "dir": str(entry.dir),
        "ancestors": [node_summary(a) for a in tree.ancestors(entry)],
        "children": [node_summary(c) for c in tree.children(entry)],
        "notes": entry.node.notes,
        "git": worktree.status(entry.node) if entry.node.git else None,
        "recipes": {k: recipes.effective(tree, entry, k) for k in recipes.RECIPE_KINDS},
        "own_recipes": {k: recipes.own(entry, k) for k in recipes.RECIPE_KINDS},
        "scripts_dir": str(scripts_dir),
        "scripts": scripts,
        "runsets": summaries,
        "runset_count": len(runsets),
        "archived_count": len(archived),
        "archived_runsets": [f"{entry.rel}/{rs.name}" for rs in archived],
        "params_differing": differing,
    }


def brief(tree: Tree, entry: Entry, *, max_runsets: int = 3, include_archived: bool = False,
          archived_only: bool = False) -> dict:
    full = build(tree, entry, max_runsets=max_runsets, include_archived=include_archived,
                 archived_only=archived_only)
    metric_keys = {"suite", "reference", "block", "configured_points", "completed_points", "points",
                   "common_points", "configured_benchmarks", "fully_completed_benchmarks",
                   "ipc_geomean_completed_benchmarks", "benchmark_mean_ipc", "lifecycle_nonzero_points",
                   "topdown_invalid_points", "self.mean_abs_err_pct", "self.rms_err_pct", "self.ipc_geomean"}
    runsets = []
    for rs in full["runsets"]:
        post = {name: {k: v for k, v in summary.items() if k in metric_keys and v is not None}
                for name, summary in rs["post"].items()}
        runsets.append({"ref": rs["ref"], "purpose": rs["purpose"], "sealed": rs["sealed"],
                        "archived": rs["archived"], "tasks": rs["tasks"], "params": rs["params"],
                        "post": post})
    return {"node": full["node"], "notes": full["notes"], "git": full["git"],
            "runsets": runsets, "runset_count": full["runset_count"],
            "archived_count": full["archived_count"], "archived_runsets": full["archived_runsets"]}


def read_script(entry: Entry, name: str) -> str:
    path = entry.dir / "scripts" / Path(name).name
    return path.read_text(errors="replace")
