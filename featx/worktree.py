"""Worktrees follow the tree: <wt_root>/<node path>, branch <git.branch_prefix><node path>
(prefix "" by default, so node wpe/overlay/smoke works on branch wpe/overlay/smoke).

Only branches featx created (`managed`) are renamed by `node mv` and deleted by
`node rm --drop-worktrees`; adopted worktrees keep their user branch. Records written
before `managed` existed count as featx's when they carry the old "featx/" prefix.

Creation goes through featx/bin/add-worktree.sh (vendored util/add-worktree-with-shared-submodules.sh) (submodules shared by
symlink). Submodules listed in settings git.submodule_worktrees (default TraceRTL) are
then replaced by a real submodule worktree on their own branch, so code changes there
are isolated too — the same layout the hand-made trace-wpe-* worktrees use.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import config, store
from .model import GitInfo, Node, SubWorktree
from .tree import Entry, Tree, TreeError, run_git


LEGACY_PREFIX = "featx/"


def default_branch(rel: str) -> str:
    return (config.settings()["git"].get("branch_prefix") or "") + rel


def managed(git) -> bool:
    """Did featx create this (GitInfo / SubWorktree) branch?"""
    if git.managed is not None:
        return git.managed
    prefix = config.settings()["git"].get("branch_prefix") or ""
    return git.branch.startswith(LEGACY_PREFIX) or bool(prefix and git.branch.startswith(prefix))


def branch_conflict(repo: str, branch: str, ignore: str | None = None) -> str | None:
    """Why `branch` can't be created in repo: it exists, or a branch is a path prefix of it
    (or under it) — git can't hold refs a and a/b at once."""
    names = set(run_git(["-C", repo, "for-each-ref", "--format=%(refname:short)", "refs/heads"],
                        check=False).split())
    names.discard(ignore or "")
    if branch in names:
        return f"branch {branch} already exists in {repo}"
    parts = branch.split("/")
    for i in range(1, len(parts)):
        if "/".join(parts[:i]) in names:
            return f"branch {'/'.join(parts[:i])} exists in {repo}, so {branch} can't be created"
    under = sorted(n for n in names if n.startswith(branch + "/"))
    if under:
        return f"branch {under[0]} exists in {repo}, so {branch} can't be created"
    return None


def default_path(rel: str) -> Path:
    return config.wt_root() / rel


def _script() -> Path:
    return config.BIN_DIR / "add-worktree.sh"


def create(tree: Tree, ref: str, *, repo: str | None = None, base: str | None = None,
           branch: str | None = None, path: str | None = None,
           submodules: list[str] | None = None, sub_base: dict[str, str] | None = None,
           adopt: bool = False) -> Entry:
    """Create (or with adopt=True, register an existing) worktree for a node."""
    entry = tree.get(ref)
    if entry.node.git and not adopt:
        raise TreeError(f"{entry.rel} already has a worktree: {entry.node.git.worktree}")
    gset = config.settings()["git"]
    repo = str(Path(repo or config.require("git.repo")).resolve())
    base = base or gset["base"]
    branch = branch or default_branch(entry.rel)
    wt_path = Path(path) if path else default_path(entry.rel)
    submodules = gset.get("submodule_worktrees", []) if submodules is None else submodules
    sub_base = sub_base or {}

    if adopt:
        if not wt_path.is_dir():
            raise TreeError(f"adopt: {wt_path} does not exist")
        branch = run_git(["-C", str(wt_path), "rev-parse", "--abbrev-ref", "HEAD"])
        info = GitInfo(repo=repo, base=base, branch=branch, worktree=str(wt_path), managed=False)
        for sub in submodules:
            sp = wt_path / sub
            if sp.is_dir() and not sp.is_symlink() and (sp / ".git").exists():
                info.submodule_worktrees[sub] = SubWorktree(
                    repo=str(Path(repo) / sub), base=sub_base.get(sub, ""),
                    branch=run_git(["-C", str(sp), "rev-parse", "--abbrev-ref", "HEAD"]), path=str(sp),
                    managed=False)
    else:
        for r in [repo] + [str(Path(repo) / s) for s in submodules]:
            why = branch_conflict(r, branch) if (Path(r) / ".git").exists() else None
            if why:
                raise TreeError(f"{why}; pass --branch <name>")
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "FEATX_TRACE_ENABLED": "1" if config.settings().get("trace_enabled", False) else "0"}
        proc = subprocess.run(["bash", str(_script()), repo, base, branch, "--path", str(wt_path)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if proc.returncode != 0:
            raise TreeError(f"add-worktree failed: {proc.stderr.strip() or proc.stdout.strip()}")
        created = Path(proc.stdout.strip().splitlines()[-1])
        info = GitInfo(repo=repo, base=base, branch=branch, worktree=str(created), managed=True)
        for sub in submodules:
            info.submodule_worktrees[sub] = _sub_worktree(repo, base, created, sub, branch, sub_base.get(sub))

    with store.file_lock(config.lock_path()):
        tree.scan()
        entry = tree.get(entry.node.id)
        entry.node.git = info
        tree.save(entry)
    return entry


def _sub_worktree(repo: str, base: str, wt: Path, sub: str, branch: str, sub_base: str | None) -> SubWorktree:
    sub_repo = Path(repo) / sub
    if not (sub_repo / ".git").exists():
        raise TreeError(f"submodule {sub} not checked out in {repo}")
    # Default base: the commit the superproject's base branch records for this submodule.
    if not sub_base:
        line = run_git(["-C", repo, "ls-tree", base, sub])
        sub_base = line.split()[2] if line else "HEAD"
    dst = wt / sub
    if dst.is_symlink():
        dst.unlink()
    elif dst.is_dir() and not any(dst.iterdir()):
        dst.rmdir()
    run_git(["-C", str(sub_repo), "worktree", "add", "-b", branch, str(dst), sub_base])
    return SubWorktree(repo=str(sub_repo), base=sub_base, branch=branch, path=str(dst), managed=True)


def plan_relocate(node: Node, rel_new: str, *, rename_branch: bool = True) -> dict:
    """What relocate() would do, without doing it (node mv --dry-run)."""
    git = node.git
    assert git is not None
    rec: dict = {}
    old = Path(git.worktree)
    if _at_default(old) and old.is_dir() and not old.is_symlink():
        new = default_path(rel_new)
        rec["worktree"] = {"from": str(old), "to": str(new)}
        if new.exists() and not new.is_symlink():
            rec["error"] = f"worktree destination exists: {new}"
    else:
        rec["worktree_kept"] = str(old)
    new_branch = default_branch(rel_new)
    if rename_branch and managed(git) and git.branch != new_branch:
        why = _rename_conflict(git, new_branch)
        if why:
            rec["branch_kept"] = f"{git.branch} ({why})"
        else:
            rec["branch"] = {"from": git.branch, "to": new_branch,
                             "submodules": [n for n, s in git.submodule_worktrees.items() if managed(s)]}
    return rec


def _rename_conflict(git, new_branch: str) -> str | None:
    for repo, old in [(git.repo, git.branch)] + [(s.repo, s.branch) for s in git.submodule_worktrees.values()
                                                 if managed(s)]:
        why = branch_conflict(repo, new_branch, ignore=old)
        if why:
            return why
    return None


def _at_default(path: Path) -> bool:
    return path.parent == config.wt_root() or str(path).startswith(str(config.wt_root()) + "/")


def relocate(node: Node, rel_new: str, *, rename_branch: bool = True) -> dict:
    """Called by Tree.mv. Moves the worktree dir if it lives at the default location for
    the old path, then `git worktree repair` (git refuses `worktree move` for worktrees
    containing submodules). Leaves a forwarding symlink at the old path."""
    git = node.git
    assert git is not None
    rec: dict = {}
    old = Path(git.worktree)
    if _at_default(old) and old.is_dir() and not old.is_symlink():
        new = default_path(rel_new)
        if new.exists() or new.is_symlink():
            if new.is_symlink():
                new.unlink()
            else:
                raise TreeError(f"worktree destination exists: {new}")
        new.parent.mkdir(parents=True, exist_ok=True)
        os.rename(old, new)
        os.symlink(new, old)
        run_git(["-C", git.repo, "worktree", "repair", str(new)])
        for sub in git.submodule_worktrees.values():
            sub_new = new / Path(sub.path).relative_to(old)
            run_git(["-C", sub.repo, "worktree", "repair", str(sub_new)])
            sub.path = str(sub_new)
        git.worktree = str(new)
        rec["worktree"] = {"from": str(old), "to": str(new)}
    new_branch = default_branch(rel_new)
    if rename_branch and managed(git) and git.branch != new_branch:
        why = _rename_conflict(git, new_branch)
        if why:
            # The move itself is fine; only the branch keeps its old name.
            rec["branch_kept"] = f"{git.branch} ({why})"
        else:
            rec["branch"] = _rename(git, new_branch)
    return rec


def _rename(git, new_branch: str) -> dict:
    rec = {"from": git.branch, "to": new_branch, "submodules": []}
    run_git(["-C", git.repo, "branch", "-m", git.branch, new_branch])
    git.managed, git.branch = True, new_branch
    for name, sub in git.submodule_worktrees.items():
        if managed(sub):
            run_git(["-C", sub.repo, "branch", "-m", sub.branch, new_branch])
            sub.managed, sub.branch = True, new_branch
            rec["submodules"].append(name)
    return rec


def rename_branch(node: Node, new_branch: str, *, dry_run: bool = False) -> dict:
    """Rename a node's featx-created branch (superproject + managed submodule worktrees),
    e.g. a pre-0.x featx/<path> branch to the current default name."""
    git = node.git
    if git is None:
        raise TreeError(f"{node.name} has no worktree")
    if not managed(git):
        raise TreeError(f"branch {git.branch} was adopted, not created by featx; rename it with git")
    if git.branch == new_branch:
        return {"from": git.branch, "to": new_branch, "unchanged": True}
    why = _rename_conflict(git, new_branch)
    if why:
        raise TreeError(why)
    if dry_run:
        return {"from": git.branch, "to": new_branch,
                "submodules": [n for n, s in git.submodule_worktrees.items() if managed(s)]}
    return _rename(git, new_branch)


def _drop_branch(repo: str, branch: str, base: str, force: bool) -> str:
    """Delete a featx branch once its worktree is gone. Branches holding commits that are not
    in `base` are kept unless forced: the commits are the only copy of that work."""
    if not run_git(["-C", repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], check=False):
        return "absent"
    if not force:
        ahead = run_git(["-C", repo, "rev-list", "--count", f"{base}..{branch}"], check=False)
        if ahead != "0":
            return f"kept ({ahead or '?'} commits not in {base})"
    run_git(["-C", repo, "branch", "-D", branch], check=False)
    return "deleted"


def _prune_dirs(paths: list[Path]) -> None:
    """Drop dangling forwarding symlinks left by `node mv`, then empty parents up to wt_root."""
    top = config.wt_root().resolve()
    for p in paths:
        if p.is_symlink() and not p.exists():
            p.unlink()
        d = p.parent if p.is_symlink() or not p.is_dir() else p
        while d.resolve() != top and str(d.resolve()).startswith(str(top) + "/"):
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent


def remove(node: Node, delete_branch: bool = False, force_branch: bool = False) -> dict:
    """Remove a node's worktree (after rm). Uncommitted work is preserved as a patch
    under the trash dir before deletion. delete_branch also drops the featx branches of the
    superproject and submodule worktrees (unmerged ones only with force_branch), the
    forwarding symlinks of earlier moves and the empty dirs left under wt_root."""
    git = node.git
    if not git:
        return {}
    wt = Path(git.worktree)
    out: dict = {"worktree": str(wt)}
    if wt.is_dir():
        patch_dir = config.tree_root() / ".trash" / "worktree-patches"
        patch_dir.mkdir(parents=True, exist_ok=True)
        tag = f"{store.today()}-{node.id[-6:]}"
        diff = run_git(["-C", str(wt), "diff", "HEAD"], check=False)
        if diff:
            (patch_dir / f"{tag}.patch").write_text(diff + "\n")
        for name, sub in git.submodule_worktrees.items():
            sdiff = run_git(["-C", sub.path, "diff", "HEAD"], check=False)
            if sdiff:
                (patch_dir / f"{tag}-{name}.patch").write_text(sdiff + "\n")
        shutil.rmtree(wt, ignore_errors=True)
    run_git(["-C", git.repo, "worktree", "prune"], check=False)
    for sub in git.submodule_worktrees.values():
        run_git(["-C", sub.repo, "worktree", "prune"], check=False)
    if delete_branch:
        branches = {}
        if managed(git):     # adopted worktrees sit on a user branch: never deleted
            branches[git.repo] = _drop_branch(git.repo, git.branch, git.base, force_branch)
        for sub in git.submodule_worktrees.values():
            if managed(sub):
                branches[sub.repo] = _drop_branch(sub.repo, sub.branch, sub.base, force_branch)
        out["branches"] = branches
        old = [Path(m["git"]["worktree"]["from"]) for m in node.moves
               if isinstance(m.get("git", {}).get("worktree"), dict)]
        _prune_dirs([wt, *old])
    return out


def status(node: Node) -> dict:
    git = node.git
    if not git:
        return {}
    out = {"worktree": git.worktree, "branch": git.branch, "repo": git.repo, "base": git.base,
           "exists": Path(git.worktree).is_dir()}
    if out["exists"]:
        out["head"] = run_git(["-C", git.worktree, "rev-parse", "--short", "HEAD"], check=False)
        porcelain = run_git(["-C", git.worktree, "status", "--porcelain", "--ignore-submodules=all"],
                            check=False)
        out["dirty"] = len(porcelain.splitlines())
        subs = {}
        for name, sub in git.submodule_worktrees.items():
            porcelain = run_git(["-C", sub.path, "status", "--porcelain"], check=False)
            subs[name] = {"branch": sub.branch, "path": sub.path,
                          "head": run_git(["-C", sub.path, "rev-parse", "--short", "HEAD"], check=False),
                          "dirty": len(porcelain.splitlines())}
        out["submodules"] = subs
        mb = run_git(["-C", git.worktree, "merge-base", git.base, "HEAD"], check=False)
        if mb:
            ahead = run_git(["-C", git.worktree, "rev-list", "--count", f"{mb}..HEAD"], check=False)
            out["ahead"] = int(ahead or 0)
            changed = run_git(["-C", git.worktree, "diff", "--name-only", "-z", "--ignore-submodules=all",
                               mb], check=False)
            skip = _submodule_paths(git.worktree)
            out["files_changed"] = len([p for p in changed.split("\0") if p and p not in skip])
    return out


MAX_COMMITS = 200
MAX_FILES = 2000
MAX_DIFF_BYTES = 256 * 1024


def _repos(node: Node) -> dict[str, tuple[str, str]]:
    """name -> (checkout path, base) for the node worktree ("" = main) and its submodule
    worktrees."""
    git = node.git
    out = {"": (git.worktree, git.base)}
    for name, sub in git.submodule_worktrees.items():
        out[name] = (sub.path, sub.base)
    return out


def _submodule_paths(path: str) -> set[str]:
    raw = run_git(["-C", path, "config", "-f", ".gitmodules", "--get-regexp", r"submodule\..*\.path"],
                  check=False)
    return {line.split(None, 1)[1] for line in raw.splitlines() if len(line.split(None, 1)) == 2}


def _z_entries(raw: str, numstat: bool) -> list[tuple[list[str], list[str]]]:
    """Parse `git diff -z --numstat|--name-status` into (fields, paths) records; renames and
    copies carry two paths."""
    toks = raw.split("\0")
    out, i = [], 0
    while i < len(toks):
        tok = toks[i]
        if not tok:
            i += 1
            continue
        if numstat:
            added, deleted, path = tok.split("\t", 2)
            if path:
                out.append(([added, deleted], [path]))
                i += 1
            else:                                   # rename: "a\td\t" then old, new
                out.append(([added, deleted], [toks[i + 1], toks[i + 2]]))
                i += 3
        else:
            if tok[0] in "RC":
                out.append(([tok], [toks[i + 1], toks[i + 2]]))
                i += 3
            else:
                out.append(([tok], [toks[i + 1]]))
                i += 2
    return out


def _repo_changes(path: str, base: str) -> dict:
    out = {"path": path, "base": base, "exists": Path(path).is_dir()}
    if not out["exists"]:
        return out
    mb = run_git(["-C", path, "merge-base", base, "HEAD"], check=False)
    if not mb:
        out["error"] = f"no merge-base between {base} and HEAD"
        return out
    out["merge_base"] = mb[:12]
    counts = run_git(["-C", path, "rev-list", "--left-right", "--count", f"{base}...HEAD"],
                     check=False).split()
    out["behind"], out["ahead"] = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
    log = run_git(["-C", path, "log", f"-{MAX_COMMITS}", "--format=%h%x09%an%x09%ar%x09%s",
                   f"{mb}..HEAD"], check=False)
    out["commits"] = [dict(zip(("sha", "author", "when", "subject"), line.split("\t", 3)))
                      for line in log.splitlines() if line]
    skip = _submodule_paths(path)
    # merge-base vs working tree: committed and uncommitted changes together
    args = ["-C", path, "diff", "-z", "-M", "--ignore-submodules=all"]
    stats = {tuple(p)[-1]: f for f, p in _z_entries(run_git(args + ["--numstat", mb], check=False), True)}
    uncommitted = {p[-1] for _, p in _z_entries(run_git(args + ["--name-status", "HEAD"], check=False),
                                                False)}
    files = []
    for (code,), paths in _z_entries(run_git(args + ["--name-status", mb], check=False), False):
        if paths[-1] in skip:
            continue
        added, deleted = stats.get(paths[-1], ["0", "0"])
        files.append({"status": code[0], "path": paths[-1], "old_path": paths[0] if len(paths) > 1 else None,
                      "added": None if added == "-" else int(added),
                      "deleted": None if deleted == "-" else int(deleted),
                      "uncommitted": paths[-1] in uncommitted})
    out["truncated"] = len(files) > MAX_FILES
    out["files"] = files[:MAX_FILES]
    untracked = run_git(["-C", path, "ls-files", "--others", "--exclude-standard", "-z"], check=False)
    untracked = [p for p in untracked.split("\0") if p and p not in skip]
    out["untracked"] = untracked[:MAX_FILES]
    out["added"] = sum(f["added"] or 0 for f in files)
    out["deleted"] = sum(f["deleted"] or 0 for f in files)
    return out


def changes(node: Node) -> dict:
    """Code that differs from the base: commits ahead, files changed since the merge-base
    (committed + uncommitted) and untracked files, for the worktree and each submodule
    worktree. Shared (symlinked) submodules are left out."""
    if not node.git:
        return {"repos": {}}
    repos = {name: _repo_changes(path, base) for name, (path, base) in _repos(node).items()}
    total = {"files": sum(len(r.get("files", [])) + len(r.get("untracked", [])) for r in repos.values()),
             "added": sum(r.get("added", 0) for r in repos.values()),
             "deleted": sum(r.get("deleted", 0) for r in repos.values()),
             "ahead": sum(r.get("ahead", 0) for r in repos.values())}
    return {"repos": repos, "total": total}


def file_diff(node: Node, sub: str, path: str) -> dict:
    """Unified diff of one changed file against the merge-base (working tree side)."""
    repos = _repos(node) if node.git else {}
    if sub not in repos:
        raise TreeError(f"no repo {sub!r} in this node's worktree")
    wt, base = repos[sub]
    info = _repo_changes(wt, base)
    known = {f["path"]: f for f in info.get("files", [])}
    if path in info.get("untracked", []):
        fp = Path(wt) / path
        text = fp.read_text(errors="replace") if fp.is_file() else ""
        diff = "".join(f"+{line}\n" for line in text.splitlines())
        diff = f"--- /dev/null\n+++ b/{path}\n" + diff
    elif path in known:
        f = known[path]
        paths = [f["old_path"], path] if f["old_path"] else [path]
        diff = run_git(["-C", wt, "diff", "-M", info["merge_base"], "--", *paths], check=False)
    else:
        raise TreeError(f"{path} is not among the changed files of {sub or 'the worktree'}")
    truncated = len(diff) > MAX_DIFF_BYTES
    return {"sub": sub, "path": path, "diff": diff[:MAX_DIFF_BYTES], "truncated": truncated}
