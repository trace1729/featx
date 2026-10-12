"""Worktrees follow the tree: <wt_root>/<node path>, branch featx/<node path>.

Creation goes through util/add-worktree-with-shared-submodules.sh (submodules shared by
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


def default_branch(rel: str) -> str:
    return config.settings()["git"].get("branch_prefix", "featx/") + rel


def default_path(rel: str) -> Path:
    return config.wt_root() / rel


def _script() -> Path:
    return config.util_dir() / "add-worktree-with-shared-submodules.sh"


def create(tree: Tree, ref: str, *, repo: str | None = None, base: str | None = None,
           branch: str | None = None, path: str | None = None,
           submodules: list[str] | None = None, sub_base: dict[str, str] | None = None,
           adopt: bool = False) -> Entry:
    """Create (or with adopt=True, register an existing) worktree for a node."""
    entry = tree.get(ref)
    if entry.node.git and not adopt:
        raise TreeError(f"{entry.rel} already has a worktree: {entry.node.git.worktree}")
    gset = config.settings()["git"]
    repo = str(Path(repo or gset["repo"]).resolve())
    base = base or gset["base"]
    branch = branch or default_branch(entry.rel)
    wt_path = Path(path) if path else default_path(entry.rel)
    submodules = gset.get("submodule_worktrees", []) if submodules is None else submodules
    sub_base = sub_base or {}

    if adopt:
        if not wt_path.is_dir():
            raise TreeError(f"adopt: {wt_path} does not exist")
        branch = run_git(["-C", str(wt_path), "rev-parse", "--abbrev-ref", "HEAD"])
        info = GitInfo(repo=repo, base=base, branch=branch, worktree=str(wt_path))
        for sub in submodules:
            sp = wt_path / sub
            if sp.is_dir() and not sp.is_symlink() and (sp / ".git").exists():
                info.submodule_worktrees[sub] = SubWorktree(
                    repo=str(Path(repo) / sub), base=sub_base.get(sub, ""),
                    branch=run_git(["-C", str(sp), "rev-parse", "--abbrev-ref", "HEAD"]), path=str(sp))
    else:
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(["bash", str(_script()), repo, base, branch, "--path", str(wt_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if proc.returncode != 0:
            raise TreeError(f"add-worktree failed: {proc.stderr.strip() or proc.stdout.strip()}")
        created = Path(proc.stdout.strip().splitlines()[-1])
        info = GitInfo(repo=repo, base=base, branch=branch, worktree=str(created))
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
    return SubWorktree(repo=str(sub_repo), base=sub_base, branch=branch, path=str(dst))


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
    prefix = config.settings()["git"].get("branch_prefix", "featx/")
    new_branch = default_branch(rel_new)
    if rename_branch and git.branch.startswith(prefix) and git.branch != new_branch:
        rec["branch"] = {"from": git.branch, "to": new_branch,
                         "submodules": [n for n, s in git.submodule_worktrees.items() if s.branch.startswith(prefix)]}
    return rec


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
    if rename_branch:
        prefix = config.settings()["git"].get("branch_prefix", "featx/")
        new_branch = default_branch(rel_new)
        if git.branch.startswith(prefix) and git.branch != new_branch:
            run_git(["-C", git.repo, "branch", "-m", git.branch, new_branch])
            rec["branch"] = {"from": git.branch, "to": new_branch}
            git.branch = new_branch
            for sub in git.submodule_worktrees.values():
                if sub.branch.startswith(prefix):
                    run_git(["-C", sub.repo, "branch", "-m", sub.branch, new_branch])
                    sub.branch = new_branch
    return rec


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
        prefix = config.settings()["git"].get("branch_prefix", "featx/")
        branches = {}
        if git.branch.startswith(prefix):     # adopted worktrees may sit on a user branch
            branches[git.repo] = _drop_branch(git.repo, git.branch, git.base, force_branch)
        for sub in git.submodule_worktrees.values():
            if sub.branch.startswith(prefix):
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
    out = {"worktree": git.worktree, "branch": git.branch, "exists": Path(git.worktree).is_dir()}
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
    return out
