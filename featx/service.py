"""Tree-side operations shared by the daemon (REST), the CLI (direct, works with the daemon
down) and the MCP server. Everything returns plain JSON-able dicts.

Scheduler operations (submit/relaunch/cancel/events) live in sched/scheduler.py and are only
reachable through the daemon, which is the single writer of tasks/ and events/.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import baselines, config, context, params as params_mod, problemlog, provenance, recipes, snapshots, store, taskstore, worktree
from .model import ACTIVE_TASK_STATES
from .tree import Tree, TreeError, purge_trash, render_tree, run_git


# ------------------------------------------------------------------ tree
def tree_rows(tree: Tree, status: dict[str, dict] | None = None) -> list[dict]:
    """Flat, depth-first rows for UIs: path, depth, kind, verdict, status + task summary."""
    status = status if status is not None else offline_status()
    rows = []

    def walk(entry, depth):
        st = status.get(entry.node.id) or {}
        all_runsets = tree.runsets(entry)
        visible = [rs for rs in all_runsets if not rs.archived]
        archived = {rs.name for rs in all_runsets if rs.archived}
        latest = st.get("runset")
        if latest in archived:
            latest = visible[-1].name if visible else None
        rows.append({"id": entry.node.id, "path": entry.rel, "name": entry.node.name, "depth": depth,
                     "kind": entry.node.kind, "verdict": entry.node.verdict, "status": entry.node.status,
                     "hypothesis": entry.node.hypothesis, "worktree": bool(entry.node.git),
                     "runsets": len(visible), "archived_count": len(archived), "latest": latest,
                     "tasks": st.get("tasks", {}), "active": st.get("active", []), "eta_s": st.get("eta_s")})
        for kid in tree.children(entry):
            walk(kid, depth + 1)

    for r in tree.roots():
        walk(r, 0)
    return rows


def offline_status() -> dict[str, dict]:
    """node id -> latest runset task states (no ETA) straight from tasks/*.json."""
    out: dict[str, dict] = {}
    for t in sorted(taskstore.load_all().values(), key=lambda t: t.created):
        d = out.setdefault(t.node_id, {"runset": None, "tasks": {}, "active": [], "eta_s": None})
        if d["runset"] != t.runset_name:
            d["runset"], d["tasks"] = t.runset_name, {}
        d["tasks"][t.kind] = t.state
        if t.state in ACTIVE_TASK_STATES:
            d["active"].append(t.kind)
    return out


def tree_text(tree: Tree, status: dict[str, dict] | None = None, root: str | None = None) -> str:
    from .sched.eta import fmt_duration
    status = status if status is not None else offline_status()
    mark = {"supported": "✔", "refuted": "✘", "inconclusive": "?", "open": ""}

    def annotate(entry):
        bits = []
        v = mark.get(entry.node.verdict, "")
        if v:
            bits.append(v)
        if entry.node.status != "active":
            bits.append(f"[{entry.node.status}]")
        st = status.get(entry.node.id)
        if st and st.get("tasks"):
            stages = " ".join(f"{k}:{s}" for k, s in st["tasks"].items())
            bits.append(f"{st['runset']} {stages}")
            if st.get("active"):
                bits.append(f"ETA {fmt_duration(st.get('eta_s'))}")
        return ("  " + "  ".join(bits)) if bits else ""

    return render_tree(tree, annotate, tree.get(root) if root else None)


def node_add(rel: str, *, hypothesis: str = "", tags: list[str] | None = None, parents: bool = False,
             worktree_create: bool = False, base: str | None = None) -> dict:
    tree = Tree()
    entry = tree.add(rel, hypothesis=hypothesis, tags=tags, parents=parents)
    out = {"id": entry.node.id, "path": entry.rel, "dir": str(entry.dir), "kind": entry.node.kind}
    if worktree_create:
        entry = worktree.create(Tree(), entry.node.id, base=base)
        out["worktree"] = entry.node.git.worktree
        out["branch"] = entry.node.git.branch
    return out


def node_context(ref: str, max_runsets: int = 3, *, include_archived: bool = False,
                 archived_only: bool = False) -> dict:
    tree = Tree()
    return context.build(tree, tree.get(ref), max_runsets=max_runsets,
                         include_archived=include_archived, archived_only=archived_only)


def node_update(ref: str, **fields) -> dict:
    entry = Tree().update(ref, **fields)
    return context.node_summary(entry)


def node_mv(src: str, dst: str, *, parents: bool = False, rename_branches: bool = True,
            dry_run: bool = False) -> dict:
    return Tree().mv(src, dst, parents=parents, rename_branches=rename_branches, dry_run=dry_run)


def node_rm(ref: str, *, force: bool = False, drop_worktrees: bool = False, drop_branches: bool = False) -> dict:
    return Tree().rm(ref, force=force, drop_worktrees=drop_worktrees, drop_branches=drop_branches)


# --------------------------------------------------------------- recipes
def recipe_get(ref: str, kind: str, *, effective: bool = True, explain: bool = False):
    tree = Tree()
    entry = tree.get(ref)
    if explain:
        return recipes.explain(tree, entry, kind)
    return recipes.effective(tree, entry, kind) if effective else recipes.own(entry, kind)


def recipe_set(ref: str, kind: str, data, *, merge: bool = False) -> dict:
    entry = Tree().get(ref)
    if merge:
        if isinstance(data, str):
            import yaml
            data = yaml.safe_load(data) or {}
        return recipes.patch_own(entry, kind, data)
    return recipes.set_own(entry, kind, data)


def recipe_set_root(kind: str, data: dict) -> dict:
    """Global recipe layer $FEATX_ROOT/recipes/<kind>.yaml."""
    store.write_yaml(config.root() / "recipes" / f"{kind}.yaml", data)
    return data


# --------------------------------------------------------------- scripts
def script_write(ref: str, name: str, content: str, executable: bool = True) -> dict:
    entry = Tree().get(ref)
    name = Path(name).name
    if not name or name.startswith("."):
        raise TreeError(f"bad script name {name!r}")
    path = entry.dir / "scripts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    store.atomic_write_text(path, content)
    if executable:
        os.chmod(path, 0o755)
    return {"path": str(path), "bytes": len(content)}


def script_read(ref: str, name: str) -> str:
    return context.read_script(Tree().get(ref), name)


# -------------------------------------------------------------- worktree
def worktree_create(ref: str, **kw) -> dict:
    entry = worktree.create(Tree(), ref, **kw)
    return {"path": entry.rel, **entry.node.git.model_dump()}


def worktree_rename_branch(ref: str, to: str | None = None, dry_run: bool = False) -> dict:
    tree = Tree()
    entry = tree.get(ref)
    with store.file_lock(config.lock_path()):
        tree.scan()
        entry = tree.get(entry.node.id)
        rec = worktree.rename_branch(entry.node, to or worktree.default_branch(entry.rel), dry_run=dry_run)
        if not dry_run and not rec.get("unchanged"):
            tree.save(entry)
    return rec


def worktree_status(ref: str) -> dict:
    entry = Tree().get(ref)
    if not entry.node.git:
        raise TreeError(f"{entry.rel} has no worktree")
    return worktree.status(entry.node)


def worktree_changes(ref: str) -> dict:
    entry = Tree().get(ref)
    if not entry.node.git:
        raise TreeError(f"{entry.rel} has no worktree")
    return worktree.changes(entry.node)


def _snapshot_of(rs):
    return (rs.provenance or {}).get("snapshot") or None


def _bin_code_source(rs) -> str | None:
    """The runset that built this runset's binary, from the bincache manifest."""
    bin = rs.bin or {}
    if not bin or bin.get("imported"):
        return None
    emu = bin.get("emu")
    if not emu:
        return None
    man = store.read_json(Path(emu).parent / "manifest.json", {})
    node, runset = man.get("node"), man.get("runset")
    return f"{node}/{runset}" if node and runset else None


def _code_source(tree: Tree, entry, rs):
    """(entry, runset, snapshot) whose code produced this runset, or None (legacy / imported)."""
    snap = _snapshot_of(rs)
    if snap:
        return entry, rs, snap
    ref = _bin_code_source(rs)
    if not ref:
        return None
    try:
        e2, rs2 = tree.find_runset(ref)
    except TreeError:
        return None
    snap2 = _snapshot_of(rs2)
    return (e2, rs2, snap2) if snap2 else None


def _prev_snapshot(tree: Tree, entry, rs):
    earlier = [r for r in tree.runsets(entry) if r.name < rs.name and _snapshot_of(r)]
    return earlier[-1] if earlier else None


def _code_repo_specs(git, snap: dict, mode: str, other: dict | None) -> list[dict]:
    """Per repo (super + every snapshotted submodule): path, base and the two sides of the diff."""
    items = [("", git.worktree, git.base, snap.get("super") or {},
              (other or {}).get("super") or {})]
    for name, info in (snap.get("submodules") or {}).items():
        sub = git.submodule_worktrees.get(name)
        items.append((name, sub.path if sub else str(Path(git.worktree) / name),
                      sub.base if sub else (info.get("base") or ""), info,
                      ((other or {}).get("submodules") or {}).get(name) or {}))
    specs = []
    for name, path, base, info, prev in items:
        commit = info.get("commit")
        if not commit:
            continue
        spec = {"name": name, "path": path, "base": base, "info": info}
        if mode == "worktree":
            spec.update(a=commit, b=None, merge_base=None)
        elif mode == "base":
            mb = info.get("merge_base") or ""
            spec.update(a=mb, b=commit, merge_base=mb)
            if not mb:
                spec["error"] = f"no merge-base between {base or '?'} and the snapshot"
        else:
            a = prev.get("commit")
            spec.update(a=a, b=commit, merge_base=info.get("merge_base"))
            if not a:
                spec["error"] = "the compared runset has no snapshot for this repo"
        specs.append(spec)
    return specs


def _code_totals(repos: dict) -> dict:
    return {"files": sum(len(r.get("files", [])) + len(r.get("untracked", [])) for r in repos.values()),
            "added": sum(r.get("added", 0) for r in repos.values()),
            "deleted": sum(r.get("deleted", 0) for r in repos.values()),
            "ahead": sum(r.get("ahead", 0) for r in repos.values())}


def _no_code(entry, rs, reason: str) -> dict:
    return {"ref": f"{entry.rel}/{rs.name}", "legacy": False, "no_code": True, "reason": reason,
            "snapshot": None, "drift": None, "source_runset": None, "repos": {}, "total": {}}


def _legacy_code(entry, rs) -> dict:
    """A runset from before snapshots: the recorded HEAD plus its src-patches/*.patch."""
    prov = rs.provenance or {}
    patches = {}
    pdir = entry.dir / rs.name / "src-patches"
    if pdir.is_dir():
        for path in sorted(pdir.iterdir()):
            if path.is_file():
                patches[path.name] = path.read_text(errors="replace")[:200000]
    return {"ref": f"{entry.rel}/{rs.name}", "legacy": True, "no_code": False, "snapshot": None,
            "drift": None, "source_runset": None, "head": (prov.get("super") or {}).get("head") or prov.get("head"),
            "provenance": prov, "patches": patches, "repos": {}, "total": {}}


def runset_code(ref: str, against: str = "base") -> dict:
    """The code a runset ran: its build snapshot diffed against the node base, the previous
    snapshot, the worktree as it is now, or another runset."""
    tree = Tree()
    entry, rs = tree.find_runset(ref)
    bin = rs.bin or {}
    if bin.get("imported") and not _snapshot_of(rs):
        return _no_code(entry, rs, "imported binary: its source is not recorded")
    source = _code_source(tree, entry, rs)
    if source is None:
        if bin.get("key"):
            return _no_code(entry, rs, f"the runset that built {bin['key']} has no snapshot "
                                       "(built before snapshots were recorded)")
        return _legacy_code(entry, rs)
    entry_s, rs_s, snap = source
    git = entry_s.node.git
    if git is None:
        return _no_code(entry, rs, "the code runset has no worktree record")
    code_ref = f"{entry_s.rel}/{rs_s.name}"
    requested = against or "base"
    mode, other, compared = requested, None, None
    if requested == "prev":
        prev = _prev_snapshot(tree, entry_s, rs_s)
        if prev is None:
            mode = "base"
        else:
            other, compared = _snapshot_of(prev), f"{entry_s.rel}/{prev.name}"
    elif requested not in ("base", "worktree"):
        try:
            _, rs2 = tree.find_runset(requested)
        except TreeError:
            raise TreeError(f"--against {requested}: not base|prev|worktree or a runset ref") from None
        other = _snapshot_of(rs2)
        if other is None:
            raise TreeError(f"runset {requested} has no code snapshot")
        mode, compared = "runset", requested
    repos = {}
    for spec in _code_repo_specs(git, snap, mode, other):
        if spec.get("error"):
            repos[spec["name"]] = {"path": spec["path"], "base": spec["base"],
                                   "exists": Path(spec["path"]).is_dir(), "error": spec["error"]}
            continue
        shape = worktree._diff_shape(spec["path"], spec["a"], spec["b"], base=spec["base"],
                                     merge_base=spec["merge_base"], with_untracked=mode == "worktree")
        if mode == "worktree":
            # files the snapshot already committed are not differences just because git still
            # calls them untracked in the live worktree
            keep = [p for p in shape.get("untracked", [])
                    if not snapshots.has_path(spec["path"], spec["info"].get("commit"), p)]
            shape["untracked"] = keep
            shape["untracked_total"] = len(keep)
        repos[spec["name"]] = shape
    return {"ref": f"{entry.rel}/{rs.name}", "against": mode, "requested_against": requested,
            "compared_runset": compared, "legacy": False, "no_code": False,
            "snapshot": snap, "drift": (rs_s.provenance or {}).get("drift"),
            "source_runset": None if code_ref == f"{entry.rel}/{rs.name}" else code_ref,
            "repos": repos, "total": _code_totals(repos)}


def runset_code_diff(ref: str, against: str, sub: str, path: str) -> dict:
    """One file's diff in the runset code view (same against= as runset_code)."""
    tree = Tree()
    entry, rs = tree.find_runset(ref)
    source = _code_source(tree, entry, rs)
    if source is None:
        raise TreeError("this runset has no code snapshot")
    entry_s, rs_s, snap = source
    git = entry_s.node.git
    requested = against or "base"
    mode, other = requested, None
    if requested == "prev":
        prev = _prev_snapshot(tree, entry_s, rs_s)
        mode = "base" if prev is None else "prev"
        other = _snapshot_of(prev) if prev else None
    elif requested not in ("base", "worktree"):
        _, rs2 = tree.find_runset(requested)
        other = _snapshot_of(rs2)
        if other is None:
            raise TreeError(f"runset {requested} has no code snapshot")
        mode = "runset"
    specs = {s["name"]: s for s in _code_repo_specs(git, snap, mode, other)}
    if sub not in specs:
        raise TreeError(f"no repo {sub!r} in this runset's snapshot")
    spec = specs[sub]
    shape = worktree._diff_shape(spec["path"], spec["a"], spec["b"], base=spec["base"],
                                 with_untracked=mode == "worktree")
    known = {f["path"]: f for f in shape.get("files", [])}
    truncated = False
    if path in (shape.get("untracked") or []):
        fp = Path(spec["path"]) / path
        text = fp.read_text(errors="replace") if fp.is_file() else ""
        diff = "".join(f"+{line}\n" for line in text.splitlines())
        diff = f"--- /dev/null\n+++ b/{path}\n" + diff
    elif path in known:
        f = known[path]
        paths = [f["old_path"], path] if f["old_path"] else [path]
        sides = [spec["a"]] + ([spec["b"]] if spec["b"] else [])
        diff = run_git(["-C", spec["path"], "diff", "-M", *sides, "--", *paths], check=False)
    else:
        raise TreeError(f"{path} is not among the changed files of {sub or 'the repo'}")
    truncated = len(diff) > worktree.MAX_DIFF_BYTES
    return {"sub": sub, "path": path, "diff": diff[:worktree.MAX_DIFF_BYTES], "truncated": truncated}


def worktree_diff(ref: str, sub: str, path: str) -> dict:
    entry = Tree().get(ref)
    if not entry.node.git:
        raise TreeError(f"{entry.rel} has no worktree")
    return worktree.file_diff(entry.node, sub, path)


# ----------------------------------------------------------- problem log
def runset_dir(ref: str) -> tuple[Path, str]:
    tree = Tree()
    entry, rs = tree.find_runset(ref)
    return entry.dir / rs.name, f"{entry.rel}/{rs.name}"


def problem_log_append(target: str, *, problem: str, solution: str, code: str = "",
                       task_id: str | None = None, author: str = "") -> dict:
    """target = runset ref (node/YYYYMMDD-n) or a node (its own problem-analysis.log)."""
    tree = Tree()
    try:
        entry, rs = tree.find_runset(target)
        d = entry.dir / rs.name
    except TreeError:
        d = tree.get(target).dir
    rec = problemlog.append(d, problem=problem, solution=solution, code=code, task_id=task_id, author=author)
    return {"file": str(d / problemlog.LOG_NAME), **rec}


# ---------------------------------------------------------------- result
def result_get(ref: str) -> dict:
    tree = Tree()
    entry, rs = tree.find_runset(ref)
    out = context.runset_summary(entry, rs, taskstore.load_all())
    out["bin"] = rs.bin
    out["provenance"] = rs.provenance
    out["recipes"] = rs.recipes
    out["overrides"] = rs.overrides
    out["params"] = params_mod.stage_params(rs.recipes, rs.bin)
    pdir = entry.dir / rs.name / "post"
    reports = {}
    if pdir.is_dir():
        for step in sorted(pdir.iterdir()):
            for name in ("report.md", "summary.md"):
                if (step / name).is_file():
                    reports[step.name] = (step / name).read_text(errors="replace")[:20000]
                    break
    out["reports"] = reports
    return out


def runsets(ref: str, *, include_archived: bool = False) -> list[dict]:
    tree = Tree()
    entry = tree.get(ref)
    tasks = taskstore.load_all()
    return [context.runset_summary(entry, rs, tasks) for rs in tree.runsets(entry)
            if include_archived or not rs.archived]


def runset_archive(ref: str, *, archived: bool = True, note: str = "") -> dict:
    """Hide (or show again) a runset: its data and binary stay, gc is unchanged. Refused while
    any of its tasks is still active."""
    tree = Tree()
    entry, rs = tree.find_runset(ref)
    tasks = taskstore.load_all()
    active = [t for t in rs.tasks if t in tasks and tasks[t].state in ACTIVE_TASK_STATES]
    if archived and active:
        raise TreeError(f"runset {entry.rel}/{rs.name} has active task(s): "
                        f"{', '.join(active)}; cancel them or wait")
    rs.archived = bool(archived)
    rs.archived_at = store.now_iso() if archived else ""
    rs.archive_note = note if archived else ""
    tree.save_runset(entry, rs)
    return context.runset_summary(entry, rs, tasks)


# ------------------------------------------------------------- checkpoints
def checkpoints(spec: str | None = None, refresh: bool = False, all_sets: bool = True) -> list[dict]:
    """Summaries of the checkpoint sets under checkpoints.roots (all_sets=False: runnable only)."""
    from . import ckpt
    rows = ckpt.catalog(refresh=refresh, spec=spec)
    return rows if all_sets else [r for r in rows if r["runnable"]]


def checkpoint_show(name: str, bench: str | None = None, refresh: bool = False) -> dict:
    """One set: its summary plus points (filtered to a benchmark) and per-bench insts."""
    from . import ckpt, points
    idx = ckpt.get(name, refresh)
    pts = {n: p for n, p in idx["points"].items() if not bench or points.bench_selected(p["bench"], [bench])}
    if bench and not pts:
        raise KeyError(f"checkpoint set {idx['name']}: no benchmark {bench!r} "
                       f"(has {', '.join(sorted({p['bench'] for p in idx['points'].values()}))})")
    return {**ckpt.summary(idx), "points": pts,
            "insts": {b: v for b, v in idx["insts"].items() if not bench or b in {p["bench"] for p in pts.values()}}}


# ------------------------------------------------------------- baselines
def baselines_list() -> dict:
    return baselines.load()


def baseline_add(name: str, root: str, description: str = "", **extra) -> dict:
    if not Path(root).is_dir():
        raise TreeError(f"baseline root {root} is not a directory")
    return baselines.add(name, root, description, **extra)


def baseline_rm(name: str, force: bool = False) -> dict:
    """Remove a baseline; refused while a recipe layer names it, unless forced."""
    if name not in baselines.load():
        raise TreeError(f"unknown baseline {name!r}")
    users = baseline_users(name)
    if users and not force:
        raise TreeError(f"baseline {name} is used by {', '.join(users)}; change them first (workloads: featx config edit) or pass --force")
    baselines.remove(name)
    return {"removed": name, "used_by": users}


def baseline_users(name: str) -> list[str]:
    """Workloads whose reference it is, and recipe layers (root / node post recipes) that
    mention it by name."""
    import re
    pat = re.compile(rf"@baseline\({re.escape(name)}\)|(^|[\s\[,'\"]){re.escape(name)}($|[\s\],'\"])", re.M)
    tree = Tree()
    out = [f"workload:{wl}" for wl, spec in (config.settings().get("workloads") or {}).items()
           if (spec or {}).get("reference") == name]
    layers = [("root", config.root() / "recipes" / "post.yaml")]
    layers += [(e.rel, e.dir / "recipes" / "post.yaml") for e in tree.by_id.values()]
    for label, path in layers:
        if path.is_file() and pat.search(path.read_text()):
            out.append(label)
    return out


def bin_import(path: str, label: str = "", mode: str | None = None) -> dict:
    if mode not in (None, "trace", "exec"):
        raise TreeError(f"--mode must be trace|exec, got {mode!r}")
    key, emu = provenance.import_binary(Path(path), label, mode)
    return {"key": key, "emu": str(emu)}


# --------------------------------------------------------------- storage
def _referenced_bins(tree: Tree) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    for entry in tree.by_id.values():
        for rs in tree.runsets(entry):
            key = (rs.bin or {}).get("key")
            if key:
                refs.setdefault(key, []).append(f"{entry.rel}/{rs.name}")
            emu = str((rs.recipes.get("run") or {}).get("emu", ""))
            if "/bincache/" in emu:
                refs.setdefault(Path(emu).parent.name, []).append(f"{entry.rel}/{rs.name}")
    return refs


def storage_report(*, worktrees: bool = False) -> dict:
    """Sizes per node (own dir, excluding child nodes), bincache with references, trash,
    compare cache; worktree sizes only on request (walking build dirs is slow on NFS)."""
    tree = Tree()
    nodes = []
    for entry in tree.by_id.values():
        own = 0
        child_dirs = {c.dir.name for c in tree.children(entry)}
        for p in entry.dir.iterdir() if entry.dir.is_dir() else []:
            if p.name in child_dirs or p.is_symlink():
                continue
            own += store.dir_size(p) if p.is_dir() else p.lstat().st_blocks * 512
        row = {"path": entry.rel, "bytes": own, "human": store.human_size(own),
               "runsets": len(tree.runsets(entry))}
        if worktrees and entry.node.git and Path(entry.node.git.worktree).is_dir():
            wt = store.dir_size(Path(entry.node.git.worktree))
            row["worktree_bytes"], row["worktree_human"] = wt, store.human_size(wt)
        nodes.append(row)
    refs = _referenced_bins(tree)
    bins = []
    bdir = config.bincache_dir()
    for d in sorted(bdir.iterdir()) if bdir.is_dir() else []:
        size = store.dir_size(d)
        man = store.read_json(d / "manifest.json", {})
        bins.append({"key": d.name, "bytes": size, "human": store.human_size(size), "refs": refs.get(d.name, []),
                     "label": man.get("label") or man.get("node"), "stored": man.get("stored")})
    trash = purge_trash(apply=False)
    cc = config.compare_dir() / "collect-cache"
    out = {
        "root": str(config.root()),
        "nodes": sorted(nodes, key=lambda r: -r["bytes"]),
        "bincache": bins,
        "trash": [{"path": str(p), "bytes": n, "human": store.human_size(n)} for p, n in trash],
        "collect_cache_bytes": store.dir_size(cc) if cc.is_dir() else 0,
    }
    total = sum(r["bytes"] for r in nodes) + sum(b["bytes"] for b in bins) + sum(n for _, n in trash) \
        + out["collect_cache_bytes"]
    out["total_bytes"], out["total_human"] = total, store.human_size(total)
    return out


def gc(*, apply: bool = False, collect_cache_days: float = 14.0, build_dirs: bool = False) -> dict:
    """Reference-aware cleanup. Candidates: unreferenced bincache entries (never ones a
    runset or baseline references), trash, stale collect-cache entries, and (with
    build_dirs) worktree build/ dirs of done/abandoned nodes. Deletes only with apply."""
    tree = Tree()
    refs = _referenced_bins(tree)
    base_bins = {str(b.get("binary", "")) for b in baselines.load().values()}
    active_emus = {str(t.spec.get("emu", "")) for t in taskstore.load_all().values() if t.state in ACTIVE_TASK_STATES}
    cands = []
    bdir = config.bincache_dir()
    for d in sorted(bdir.iterdir()) if bdir.is_dir() else []:
        emu = str(d / "emu")
        if d.name in refs or emu in base_bins or emu in active_emus:
            continue
        cands.append({"kind": "bincache", "path": str(d), "bytes": store.dir_size(d)})
    for p, n in purge_trash(apply=False):
        cands.append({"kind": "trash", "path": str(p), "bytes": n})
    cc = config.compare_dir() / "collect-cache"
    cutoff = time.time() - collect_cache_days * 86400
    for d in sorted(cc.iterdir()) if cc.is_dir() else []:
        if d.stat().st_mtime < cutoff:
            cands.append({"kind": "collect-cache", "path": str(d), "bytes": store.dir_size(d)})
    if build_dirs:
        for entry in tree.by_id.values():
            if entry.node.git and entry.node.status in ("done", "abandoned"):
                b = Path(entry.node.git.worktree) / "build"
                if b.is_dir():
                    cands.append({"kind": "worktree-build", "path": str(b), "bytes": store.dir_size(b),
                                  "node": entry.rel})
    if apply:
        for c in cands:
            shutil.rmtree(c["path"], ignore_errors=True)
        store.append_jsonl(config.root() / "tree-log.jsonl",
                           {"time": store.now_iso(), "op": "gc", "removed": [c["path"] for c in cands]})
    total = sum(c["bytes"] for c in cands)
    for c in cands:
        c["human"] = store.human_size(c["bytes"])
    return {"applied": apply, "candidates": cands, "total_bytes": total, "total_human": store.human_size(total)}
