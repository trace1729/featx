"""Node context bundle: everything an LLM (or human) needs to continue work on a node."""

from __future__ import annotations

from pathlib import Path

from . import recipes, store, taskstore, worktree
from .tree import Entry, Tree


def node_summary(entry: Entry) -> dict:
    n = entry.node
    return {"id": n.id, "path": entry.rel, "kind": n.kind, "hypothesis": n.hypothesis,
            "verdict": n.verdict, "verdict_note": n.verdict_note, "status": n.status,
            "evidence": n.evidence, "tags": n.tags}


def runset_summary(entry: Entry, rs, tasks: dict) -> dict:
    rdir = entry.dir / rs.name
    out = {"ref": f"{entry.rel}/{rs.name}", "id": rs.id, "purpose": rs.purpose, "created": rs.created,
           "sealed": rs.sealed, "derived_from": rs.derived_from, "dir": str(rdir), "tasks": []}
    for tid in rs.tasks:
        t = tasks.get(tid)
        if t:
            out["tasks"].append({"id": t.id, "kind": t.kind, "name": t.name, "state": t.state,
                                 "counts": t.counts(), "failure": (t.failure or {}).get("signature")})
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


def build(tree: Tree, entry: Entry, *, max_runsets: int = 3) -> dict:
    tasks = taskstore.load_all()
    scripts_dir = entry.dir / "scripts"
    scripts = sorted(p.name for p in scripts_dir.iterdir()) if scripts_dir.is_dir() else []
    runsets = tree.runsets(entry)
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
        "runsets": [runset_summary(entry, rs, tasks) for rs in runsets[-max_runsets:]],
        "runset_count": len(runsets),
    }


def read_script(entry: Entry, name: str) -> str:
    path = entry.dir / "scripts" / Path(name).name
    return path.read_text(errors="replace")
