"""Git snapshot refs: the exact source tree of a build.

A build compiles the live worktree, which moves on. `snapshot()` records HEAD plus the tracked
and untracked (but not ignored) changes as a commit, without touching the user's index or
worktree, and points `refs/featx/runsets/<runset id>` at it. The ref keeps the commit alive
across rebases and `git gc`, and creates no branch (a normal `git push` does not push it;
`git push --mirror` would).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .provenance import MAX_UNTRACKED_BYTES, MAX_UNTRACKED_FILES, submodule_paths

REF_PREFIX = "featx/runsets/"


def _git(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, check=False, env=env)


def _ok(proc: subprocess.CompletedProcess, what: str) -> str:
    if proc.returncode != 0:
        raise RuntimeError(f"snapshot: git {what} failed: {proc.stderr.strip()[-300:]}")
    return proc.stdout.strip()


def _untracked(repo: Path) -> list[str]:
    proc = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted(p for p in proc.stdout.split("\0") if p)


def snapshot(repo_path: str | Path, ref_name: str) -> dict:
    """Commit the worktree state under refs/<ref_name> and return what it took.

    A clean tree points the ref at HEAD instead of creating an empty commit. Untracked files
    are capped like provenance (8 MiB per file, 2000 files); the rest is listed in `skipped`.
    """
    repo = Path(repo_path)
    head = _git(repo, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        return {"error": "not a git checkout"}
    head_sha = head.stdout.strip()
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    tmp = Path(tempfile.mkdtemp(prefix="featx-snapshot-"))
    index = tmp / "index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index),
           "GIT_AUTHOR_NAME": "featx", "GIT_AUTHOR_EMAIL": "featx@localhost",
           "GIT_COMMITTER_NAME": "featx", "GIT_COMMITTER_EMAIL": "featx@localhost"}
    try:
        _ok(_git(repo, "read-tree", "HEAD", env=env), "read-tree")
        _ok(_git(repo, "add", "-u", "--", ".", env=env), "add -u")
        untracked: list[str] = []
        skipped: list[dict] = []
        names = _untracked(repo)
        for i, name in enumerate(names):
            path = repo / name
            if i >= MAX_UNTRACKED_FILES:
                skipped.append({"path": name, "reason": f"more than {MAX_UNTRACKED_FILES} untracked files"})
                continue
            try:
                if not path.is_symlink() and (not path.is_file()
                                              or path.stat().st_size > MAX_UNTRACKED_BYTES):
                    skipped.append({"path": name,
                                    "reason": f"larger than {MAX_UNTRACKED_BYTES // (1 << 20)} MiB"})
                    continue
            except OSError:
                skipped.append({"path": name, "reason": "unreadable"})
                continue
            untracked.append(name)
        for start in range(0, len(untracked), 200):
            _ok(_git(repo, "add", "-f", "--", *untracked[start:start + 200], env=env), "add")
        tree = _ok(_git(repo, "write-tree", env=env), "write-tree")
        head_tree = _ok(_git(repo, "rev-parse", "HEAD^{tree}"), "rev-parse HEAD^{tree}")
        if tree == head_tree:
            commit = head_sha
        else:
            commit = _ok(_git(repo, "commit-tree", tree, "-p", head_sha, "-m",
                              f"featx snapshot {ref_name}", env=env), "commit-tree")
        _ok(_git(repo, "update-ref", f"refs/{ref_name}", commit), "update-ref")
        return {"commit": commit, "head": head_sha, "branch": branch, "clean": tree == head_tree,
                "untracked": untracked, "skipped": skipped}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _merge_base(repo: Path, base: str, commit: str) -> str:
    if not base or not commit:
        return ""
    return _git(repo, "merge-base", base, commit).stdout.strip()


def _recorded_base(workdir: Path, super_base: str, sub: str) -> str:
    """The submodule commit the superproject's base branch records."""
    if not super_base:
        return ""
    line = _git(workdir, "ls-tree", super_base, sub).stdout.strip()
    return line.split()[2] if len(line.split()) >= 3 else ""


def worktree_snapshot(workdir: str | Path, ref_name: str, *, super_base: str = "",
                      sub_bases: dict[str, str] | None = None) -> dict:
    """snapshot() the superproject and every submodule the build covers (dedicated worktrees and
    shared/symlinked ones alike, i.e. everything `worktree_state` covers)."""
    workdir = Path(workdir)
    out: dict = {"super": {}, "submodules": {}}
    sup = snapshot(workdir, ref_name)
    sup["base"] = super_base
    sup["merge_base"] = _merge_base(workdir, super_base, sup.get("commit", ""))
    out["super"] = sup
    for sub in submodule_paths(workdir):
        path = workdir / sub
        if not (path / ".git").exists():
            continue
        info = snapshot(path, ref_name)
        base = (sub_bases or {}).get(sub) or _recorded_base(workdir, super_base, sub)
        info["base"] = base
        info["merge_base"] = _merge_base(path, base, info.get("commit", ""))
        info["shared"] = path.is_symlink()
        out["submodules"][sub] = info
    return out


def drop_ref(repo: str | Path, runset_id: str) -> bool:
    """Delete refs/featx/runsets/<id> (node rm --drop-worktrees)."""
    proc = _git(Path(repo), "update-ref", "-d", f"refs/{REF_PREFIX}{runset_id}")
    return proc.returncode == 0


def has_path(repo: str | Path, commit: str, path: str) -> bool:
    """Is `path` committed in `commit`? (Filters the worktree's untracked list in the code view.)"""
    if not commit:
        return False
    return _git(Path(repo), "cat-file", "-e", f"{commit}:{path}").returncode == 0
