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

from . import baselines, coderec, config, context, problemlog, provenance, recipes, store, taskstore, worktree
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
                 archived_only: bool = False, detail: str = "summary") -> dict:
    tree = Tree()
    return context.build(tree, tree.get(ref), max_runsets=max_runsets,
                         include_archived=include_archived, archived_only=archived_only,
                         detail=detail)


def node_brief(ref: str, max_runsets: int = 3, *, include_archived: bool = False,
               archived_only: bool = False, recipes: bool = False) -> dict:
    tree = Tree()
    entry = tree.get(ref)
    out = context.brief(tree, entry, max_runsets=max_runsets,
                        include_archived=include_archived, archived_only=archived_only)
    if recipes:     # the usual reason to escalate to --full: the effective recipe values
        from .recipes import RECIPE_KINDS, effective      # the parameter shadows the module name
        out["recipes"] = {kind: effective(tree, entry, kind) for kind in RECIPE_KINDS}
    return out


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


def worktree_set_base(ref: str, base: str, sub: str = "") -> dict:
    tree = Tree()
    entry = tree.get(ref)
    if not entry.node.git:
        raise TreeError(f"{entry.rel} has no worktree")
    with store.file_lock(config.lock_path()):
        tree.scan()
        entry = tree.get(entry.node.id)
        git = entry.node.git
        if sub:
            if sub not in git.submodule_worktrees:
                raise TreeError(f"{entry.rel} has no {sub!r} submodule worktree")
            target = git.submodule_worktrees[sub]
        else:
            target = git
        path = target.path if sub else git.worktree
        target.base = worktree.validate_base(path, base, label=f"{sub or 'worktree'} base")
        tree.save(entry)
    return worktree.status(entry.node)


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


def _bin_code_source(rs) -> str | None:
    """The runset that built this runset's binary, from the bincache manifest."""
    bin = rs.bin or {}
    emu = bin.get("emu")
    if not emu:
        return None
    man = store.read_json(Path(emu).parent / "manifest.json", {}) or {}
    node, runset = man.get("node"), man.get("runset")
    return f"{node}/{runset}" if node and runset else None


def _record_of(entry, rs) -> dict | None:
    """The code record a build wrote: per repo HEAD + patch under <runset>/code/."""
    code = (rs.provenance or {}).get("code") or {}
    if not (code.get("super") or {}).get("head"):
        return None
    return {"legacy": False, "dir": entry.dir / rs.name / "code", "super": code["super"],
            "submodules": code.get("submodules") or {}}


def _legacy_record(entry, rs) -> dict | None:
    """A runset from before code records: the HEADs it recorded at submit plus src-patches/ (the
    tracked changes at submit; its untracked files were never saved). Runsets of the short-lived
    git-snapshot scheme (provenance.snapshot) are read this way too."""
    prov = rs.provenance or {}
    sup = prov.get("super") or {}
    head = sup.get("head") or prov.get("head")
    if not head:
        return None
    pdir = entry.dir / rs.name / "src-patches"

    def info(name: str, i: dict) -> dict:
        pn = coderec.patch_name(name)
        return {"head": i.get("head"), "branch": i.get("branch"), "dirty": i.get("dirty"),
                "shared": i.get("shared", False), "patch": pn if (pdir / pn).is_file() else None,
                "unsaved_untracked": i.get("untracked_files", 0)}
    return {"legacy": True, "dir": pdir,
            "super": info("", {**sup, "head": head, "dirty": sup.get("dirty", prov.get("dirty"))}),
            "submodules": {n: info(n, i) for n, i in (prov.get("submodules") or {}).items() if (i or {}).get("head")}}


def _code_of(tree: Tree, entry, rs):
    """(entry, runset, record) behind a runset's binary: its own build record, the record of the
    runset that built its --bin, or else the HEADs recorded by that (legacy) build runset or by
    this runset itself. None for imported binaries and runsets that recorded nothing."""
    own = _record_of(entry, rs)
    if own is not None:
        return entry, rs, own
    candidates = []
    ref2 = _bin_code_source(rs)
    if ref2 and ref2 != f"{entry.rel}/{rs.name}":
        try:
            candidates.append(tree.find_runset(ref2))
        except TreeError:
            pass
    for e2, rs2 in candidates:
        rec = _record_of(e2, rs2)
        if rec is not None:
            return e2, rs2, rec
    if not (rs.bin or {}).get("imported"):
        candidates.append((entry, rs))
    for e2, rs2 in candidates:
        rec = _legacy_record(e2, rs2)
        if rec is not None:
            return e2, rs2, rec
    return None


def _prev_code(tree: Tree, entry, rs):
    """The closest earlier runset of the node with recorded code (record or legacy HEADs)."""
    for r in reversed([r for r in tree.runsets(entry) if r.name < rs.name]):
        code = _code_of(tree, entry, r)
        if code is not None:
            return r, code[2]
    return None


def _resolve_against(tree: Tree, entry_s, rs_s, requested: str):
    """(mode, other record, compared runset ref) for against=base|prev|worktree|<runset>."""
    if requested == "prev":
        found = _prev_code(tree, entry_s, rs_s)
        if found is None:
            return "base", None, None
        prev, rec = found
        return "prev", rec, f"{entry_s.rel}/{prev.name}"
    if requested in ("base", "worktree"):
        return requested, None, None
    try:
        e2, rs2 = tree.find_runset(requested)
    except TreeError:
        raise TreeError(f"--against {requested}: not base|prev|worktree or a runset ref") from None
    code = _code_of(tree, e2, rs2)
    if code is None:
        raise TreeError(f"runset {requested} has no recorded code")
    return "runset", code[2], requested


def _patch_bytes(rec: dict, info: dict, lost: list[str]) -> bytes | None:
    """The saved patch of one repo; a recorded but missing file is noted in `lost`."""
    name = info.get("patch")
    if not name:
        return None
    path = Path(rec["dir"]) / name
    if not path.is_file():
        lost.append(name)
        return None
    return path.read_bytes()


NOT_IN_BASE = "not in the node base (the submodule was added on this branch)"


def _code_repo_specs(git, rec: dict, mode: str, other: dict | None) -> list[dict]:
    """Per repo (super + every recorded submodule): path, base and the two sides of the diff,
    each side = (commit, patch bytes or None)."""
    items = [("", git.worktree, git.base, rec.get("super") or {},
              (other or {}).get("super") or {})]
    for name, info in (rec.get("submodules") or {}).items():
        sub = git.submodule_worktrees.get(name)
        # a shared submodule's base is the commit the superproject's base records
        base = sub.base if sub else (info.get("base") or coderec.recorded_base(Path(git.worktree), git.base, name))
        items.append((name, sub.path if sub else str(Path(git.worktree) / name), base, info,
                      ((other or {}).get("submodules") or {}).get(name) or {}))
    specs = []
    for name, path, base, info, prev in items:
        head = info.get("head")
        if not head:
            continue
        base = worktree.resolved_base(path, base)
        lost: list[str] = []
        spec = {"name": name, "path": path, "base": base, "info": info,
                "b": (head, _patch_bytes(rec, info, lost)), "lost": lost}
        if mode == "worktree":
            spec["a"], spec["b"] = spec["b"], None      # b = the live worktree, recorded on demand
        elif mode == "base" and not base:
            # a submodule added on the node's branch has no base to diff: its HEAD -> its record
            spec.update(a=(head, None), note=f"{NOT_IN_BASE}: only its uncommitted changes are shown")
            if not Path(path).is_dir():
                spec["error"] = NOT_IN_BASE
        elif mode == "base":
            # Old records used merge-base to preserve their original comparison point. New
            # records have a base commit and need no such fallback.
            base = info.get("merge_base") or base
            spec.update(a=(base, None), base=base)
        else:
            spec["a"] = (prev.get("head"), _patch_bytes(other, prev, lost)) if prev.get("head") else (None, None)
            if not prev.get("head"):
                spec["error"] = "the compared runset recorded nothing for this repo"
        # Recorded commits may since have been rebased away and pruned.
        commits = [s[0] for s in (spec.get("a"), spec.get("b")) if s and s[0]]
        missing = [c for c in commits if Path(path).is_dir() and run_git(
            ["-C", path, "cat-file", "-t", c], check=False).strip() != "commit"]
        if missing:
            spec["error"] = NOT_IN_BASE if mode == "base" and not base else f"commit {missing[0][:12]} is no longer in this repo"
        specs.append(spec)
    return specs


def _record_view(info: dict, rec: dict) -> dict:
    """What the record holds for one repo (the code tab's record table)."""
    out = {k: info.get(k) for k in ("head", "branch", "dirty", "shared", "files", "added", "deleted")}
    out["patch"] = info.get("patch")
    out["untracked"] = len(info.get("untracked") or [])
    out["omitted"] = info.get("omitted") or []
    out["unsaved_untracked"] = info.get("unsaved_untracked", 0)
    if rec.get("legacy") and info.get("patch"):
        out.update(coderec.patch_text_stat(_patch_bytes(rec, info, []) or b""))
    return out


def _with_trees(spec: dict, fn):
    """Run fn(trees, a_tree, b_tree, commits, uncommitted, extra) with both sides rebuilt as trees
    in a throwaway object dir. A patch that no longer applies falls back to that side's commit
    and is reported as `patch_error`."""
    path = spec["path"]
    extra: dict = {}
    if spec["lost"]:
        extra["patch_error"] = f"saved patch {spec['lost'][0]} is missing: committed code only"
    b = spec["b"]
    if b is None:                                   # the live worktree, recorded now (not saved)
        live = run_git(["-C", path, "rev-parse", "--verify", "HEAD"], check=False).strip()
        patch, _, omitted = coderec.repo_patch(path)
        b = (live, patch)
        extra["omitted"] = omitted
    else:
        extra["omitted"] = spec["info"].get("omitted") or []
    with coderec.Trees(path) as trees:
        sides = []
        for label, (commit, patch) in (("a", spec["a"]), ("b", b)):
            try:
                sides.append(trees.tree(commit, patch))
            except RuntimeError as error:
                extra.setdefault("patch_error", f"{label} side patch does not apply ({error}): committed code only")
                sides.append(commit)
        uncommitted = set(coderec.patch_files(path, b[1] or b""))
        return fn(trees, sides[0], sides[1], (spec["a"][0], b[0]), uncommitted, extra)


def _code_totals(repos: dict) -> dict:
    return {"files": sum(len(r.get("files", [])) for r in repos.values()),
            "added": sum(r.get("added", 0) for r in repos.values()),
            "deleted": sum(r.get("deleted", 0) for r in repos.values()),
            "ahead": sum(r.get("ahead", 0) for r in repos.values())}


def _no_code(entry, rs, reason: str) -> dict:
    return {"ref": f"{entry.rel}/{rs.name}", "legacy": False, "no_code": True, "reason": reason,
            "record": None, "drift": None, "source_runset": None, "repos": {}, "total": {}}


def _patches_only(entry, rs) -> dict:
    """A runset that saved src-patches but recorded no HEAD: the raw patches."""
    patches = {}
    pdir = entry.dir / rs.name / "src-patches"
    for path in sorted(pdir.iterdir()) if pdir.is_dir() else []:
        if path.is_file():
            patches[path.name] = path.read_text(errors="replace")[:200000]
    return {"ref": f"{entry.rel}/{rs.name}", "legacy": True, "no_code": False, "record": None,
            "drift": None, "source_runset": None, "patches": patches, "repos": {}, "total": {}}


def runset_code(ref: str, against: str = "base") -> dict:
    """The code a runset ran (the HEADs its build recorded plus the saved uncommitted patches),
    diffed against the node base, the previous runset, the worktree as it is now, or another
    runset. A legacy runset (from before code records) uses the HEADs recorded at submit and its
    src-patches. Files a saved patch adds are marked `uncommitted`."""
    tree = Tree()
    entry, rs = tree.find_runset(ref)
    source = _code_of(tree, entry, rs)
    if source is None:
        if (rs.bin or {}).get("imported"):
            return _no_code(entry, rs, "imported binary: its source is not recorded")
        if (entry.dir / rs.name / "src-patches").is_dir():
            return _patches_only(entry, rs)
        return _no_code(entry, rs, "no code record and no legacy source recorded")
    entry_s, rs_s, rec = source
    git = entry_s.node.git
    if git is None:
        return _no_code(entry, rs, "the code runset has no worktree record")
    code_ref = f"{entry_s.rel}/{rs_s.name}"
    requested = against or "base"
    mode, other, compared = _resolve_against(tree, entry_s, rs_s, requested)
    repos, record = {}, {}
    for spec in _code_repo_specs(git, rec, mode, other):
        record[spec["name"]] = _record_view(spec["info"], rec)
        if spec.get("error") or not Path(spec["path"]).is_dir():
            repos[spec["name"]] = {"path": spec["path"], "base": spec["base"], "exists": Path(spec["path"]).is_dir(),
                                   "error": spec.get("error") or "the checkout no longer exists"}
            continue

        def shape(trees, ta, tb, commits, uncommitted, extra, spec=spec):
            out = worktree._diff_shape(spec["path"], ta, tb, base=spec["base"] if mode == "base" else "",
                                       commits=commits, env=trees.env, uncommitted=uncommitted)
            return {**out, **extra, **({"note": spec["note"]} if spec.get("note") else {})}
        repos[spec["name"]] = _with_trees(spec, shape)
    return {"ref": f"{entry.rel}/{rs.name}", "against": mode, "requested_against": requested,
            "compared_runset": compared, "legacy": rec["legacy"], "no_code": False,
            "record": record, "drift": (rs_s.provenance or {}).get("drift"),
            "source_runset": None if code_ref == f"{entry.rel}/{rs.name}" else code_ref,
            "compared_legacy": bool((other or {}).get("legacy")),
            "repos": repos, "total": _code_totals(repos)}


def runset_code_diff(ref: str, against: str, sub: str, path: str) -> dict:
    """One file's diff in the runset code view (same against= as runset_code)."""
    tree = Tree()
    entry, rs = tree.find_runset(ref)
    source = _code_of(tree, entry, rs)
    if source is None:
        raise TreeError("this runset has no recorded code")
    entry_s, rs_s, rec = source
    mode, other, _ = _resolve_against(tree, entry_s, rs_s, against or "base")
    specs = {s["name"]: s for s in _code_repo_specs(entry_s.node.git, rec, mode, other)}
    if sub not in specs:
        raise TreeError(f"no repo {sub!r} in this runset's code record")
    spec = specs[sub]
    if spec.get("error"):
        raise TreeError(spec["error"])

    def one(trees, ta, tb, commits, uncommitted, extra):
        shape = worktree._diff_shape(spec["path"], ta, tb, commits=commits, env=trees.env, uncommitted=uncommitted)
        known = {f["path"]: f for f in shape.get("files", [])}
        if path not in known:
            raise TreeError(f"{path} is not among the changed files of {sub or 'the repo'}")
        f = known[path]
        paths = [f["old_path"], path] if f["old_path"] else [path]
        return run_git(["-C", spec["path"], "diff", "-M", ta, tb, "--", *paths], check=False, env=trees.env)
    diff = _with_trees(spec, one)
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
RESULT_SECTIONS = ("provenance", "recipes", "overrides", "reports")
FAILED_POINTS_SHOWN = 20


def result_get(ref: str, *, detail: str = "summary", sections: list[str] | None = None) -> dict:
    """Runset summary. detail="summary" keeps what a review decides on — tasks, post metrics,
    code, review state, failures — and replaces the submit-time echo (recipes/overrides, the
    per-submodule provenance, 145 benchmark names, report text) with counts plus a hint;
    `sections` adds any of RESULT_SECTIONS back."""
    if detail not in ("full", "summary"):
        raise ValueError(f"detail must be full|summary (got {detail!r})")
    bad = [name for name in sections or [] if name not in RESULT_SECTIONS]
    if bad:
        raise ValueError(f"unknown section(s) {bad}; pick from {list(RESULT_SECTIONS)}")
    tree = Tree()
    entry, rs = tree.find_runset(ref)
    tasks = taskstore.load_all()
    out = context.runset_summary(entry, rs, tasks)
    out["bin"] = rs.bin
    out["provenance"] = rs.provenance
    out["recipes"] = rs.recipes
    out["overrides"] = rs.overrides
    pdir = entry.dir / rs.name / "post"
    reports = {}
    if pdir.is_dir():
        for step in sorted(pdir.iterdir()):
            for name in ("report.md", "summary.md"):
                if (step / name).is_file():
                    reports[step.name] = (step / name).read_text(errors="replace")[:20000]
                    break
    out["reports"] = reports
    failed = []
    for tid in rs.tasks:
        task = tasks.get(tid)
        for job in (task.jobs if task else []):
            if job.state == "failed":
                failure = job.failure or {}
                failed.append({"task": task.kind, "point": job.name, "rc": job.rc,
                               "failure": failure.get("signature") or failure.get("summary")})
    out["failures"] = failed[:FAILED_POINTS_SHOWN]
    out["failures_total"] = len(failed)
    if detail == "summary":
        full = {name: out[name] for name in RESULT_SECTIONS}
        out["provenance"] = _provenance_digest(full["provenance"] or {})
        for name in ("recipes", "overrides", "reports"):
            out.pop(name)
        digest_run_params(out)
        for name in sections or []:
            out[name] = full[name]
        missing = " ".join(f"--{n}" for n in RESULT_SECTIONS if n not in out)
        out["sections_hint"] = f"featx result {ref} {missing}".rstrip()
    return out


def digest_run_params(out: dict) -> None:
    """`benchmarks` is a list of names, one per benchmark; the summary keeps only the count."""
    run = (out.get("params") or {}).get("run")
    if isinstance(run, dict) and isinstance(run.get("benchmarks"), list):
        run["benchmarks"] = len(run["benchmarks"])


def _provenance_digest(prov: dict) -> dict:
    """Per-repo provenance shrinks to where the code was and whether anything is dirty."""
    subs = prov.get("submodules") or {}
    out = {"super": prov.get("super") or {}}
    if subs:
        out["submodules"] = len(subs)
        out["submodules_dirty"] = sorted(name for name, rec in subs.items() if (rec or {}).get("dirty"))
    if prov.get("saved_patches"):
        out["saved_patches"] = len(prov["saved_patches"])
    if set(prov) - {"super", "submodules", "saved_patches"}:
        out["keys"] = sorted(set(prov) - {"super", "submodules", "saved_patches"})
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
