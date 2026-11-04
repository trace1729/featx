"""Code records: what a build compiled, as HEAD + a saved patch per repo.

A build compiles the live worktree, which moves on. `record()` keeps, for the superproject and
every submodule, the HEAD it was on plus its uncommitted changes as `<runset>/code/<repo>.patch`
(tracked edits and the small untracked files). Nothing is committed and the repos are not
written to: views rebuild a side's tree in a throwaway object directory (`Trees`).

Untracked files are kept only while they look like source. A wholly untracked directory with more
than OMIT_DIR_FILES files (a stray build output like `build-vlt-1027/`), a file over
MAX_FILE_BYTES, or anything past MAX_SAVED_FILES / MAX_SAVED_BYTES is listed as `omitted`
instead of being saved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .provenance import submodule_paths

OMIT_DIR_FILES = 50
MAX_FILE_BYTES = 1 << 20
MAX_SAVED_FILES = 200
MAX_SAVED_BYTES = 8 << 20
_DIFF = ["--binary", "--no-ext-diff", "--no-color", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/"]


def _git(repo: Path, *args: str, env: dict | None = None, input: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          input=input, check=False, env=env)


def _out(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8", "replace").strip()


def patch_name(sub: str) -> str:
    """File name of a repo's patch (same naming as the submit-time src-patches/)."""
    return "super.patch" if not sub else sub.replace("/", "__") + ".patch"


def _count_files(path: Path, limit: int) -> tuple[int, int]:
    """(files, bytes) under a directory, counting stops past `limit` files."""
    files = size = 0
    for dirpath, _, names in os.walk(path):
        for name in names:
            files += 1
            try:
                size += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
            if files > limit:
                return files, size
    return files, size


def untracked_plan(repo: str | Path) -> tuple[list[str], list[dict]]:
    """(untracked files to save, omitted entries) of a checkout; ignored files are never listed."""
    repo = Path(repo)
    raw = _out(_git(repo, "ls-files", "--others", "--exclude-standard", "--directory", "-z"))
    keep: list[str] = []
    omitted: list[dict] = []
    candidates: list[str] = []
    for entry in sorted(p for p in raw.split("\0") if p):
        if entry.endswith("/"):
            files, _ = _count_files(repo / entry, OMIT_DIR_FILES)
            if files > OMIT_DIR_FILES:
                omitted.append({"path": entry, "files": files, "reason": f"directory of more than {OMIT_DIR_FILES} files"})
                continue
            inner = _out(_git(repo, "ls-files", "--others", "--exclude-standard", "-z", "--", entry))
            candidates.extend(sorted(p for p in inner.split("\0") if p))
        else:
            candidates.append(entry)
    total = 0
    for name in candidates:
        path = repo / name
        try:
            size = path.lstat().st_size
        except OSError:
            omitted.append({"path": name, "reason": "unreadable"})
            continue
        if path.is_symlink() or not path.is_file():
            omitted.append({"path": name, "reason": "not a regular file"})
        elif size > MAX_FILE_BYTES:
            omitted.append({"path": name, "reason": f"larger than {MAX_FILE_BYTES >> 20} MiB"})
        elif len(keep) >= MAX_SAVED_FILES or total + size > MAX_SAVED_BYTES:
            omitted.append({"path": name, "reason": "over the untracked budget"})
        else:
            keep.append(name)
            total += size
    return keep, omitted


def repo_patch(repo: str | Path) -> tuple[bytes, list[str], list[dict]]:
    """(patch, untracked files in it, omitted) for HEAD -> the checkout as it is now. Nested
    submodule pointers are left out, as in the bincache key (each recorded submodule has its own)."""
    repo = Path(repo)
    parts = [_git(repo, "diff", "HEAD", *_DIFF, "--ignore-submodules=all").stdout]
    saved, omitted = untracked_plan(repo)
    for name in saved:
        # --no-index exits 1 when the files differ, which they always do here
        parts.append(_git(repo, "diff", "--no-index", *_DIFF, "/dev/null", name).stdout)
    return b"".join(parts), saved, omitted


def patch_files(repo: str | Path, patch: bytes) -> dict[str, tuple[int, int]]:
    """{path (new side): (added, deleted)} of a patch; binary files count 0/0."""
    if not patch:
        return {}
    raw = _git(Path(repo), "apply", "--numstat", "-z", "-", input=patch).stdout.decode("utf-8", "replace")
    out: dict[str, tuple[int, int]] = {}
    fields = raw.split("\0")
    i = 0
    num = lambda v: int(v) if v.isdigit() else 0  # noqa: E731  ("-" for binary files)
    while i < len(fields) and fields[i]:
        added, deleted, path = (fields[i].split("\t", 2) + ["", ""])[:3]
        if not path:                 # a rename: "a\td\t\0old\0new\0"
            path = fields[i + 2] if i + 2 < len(fields) else ""
            i += 2
        out[path] = (num(added), num(deleted))
        i += 1
    return out


def numstat(repo: str | Path, patch: bytes) -> dict:
    files = patch_files(repo, patch)
    return {"files": len(files), "added": sum(a for a, _ in files.values()),
            "deleted": sum(d for _, d in files.values())}


def patch_text_stat(patch: bytes) -> dict:
    """files / added / deleted counted from a patch's text (no git; cheap enough for tables)."""
    files = added = deleted = 0
    for line in patch.split(b"\n"):
        if line.startswith(b"diff --git "):
            files += 1
        elif line.startswith(b"+") and not line.startswith(b"+++ "):
            added += 1
        elif line.startswith(b"-") and not line.startswith(b"--- "):
            deleted += 1
    return {"files": files, "added": added, "deleted": deleted}


def recorded_base(workdir: Path, super_base: str, sub: str) -> str:
    """The submodule commit the superproject's base commit records."""
    if not super_base:
        return ""
    line = _out(_git(workdir, "ls-tree", super_base, sub))
    return line.split()[2] if len(line.split()) >= 3 else ""


def _record_repo(path: Path, dest: Path, sub: str, base: str) -> dict:
    head = _git(path, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        return {"error": "not a git checkout"}
    commit = _out(head)
    base = (_out(_git(path, "rev-parse", "--verify", f"{base}^{{commit}}")) or base) if base else ""
    patch, saved, omitted = repo_patch(path)
    info = {"head": commit, "branch": _out(_git(path, "rev-parse", "--abbrev-ref", "HEAD")),
            "base": base, "dirty": bool(patch),
            "patch": None, "untracked": saved, "omitted": omitted, **numstat(path, patch)}
    if patch:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / patch_name(sub)).write_bytes(patch)
        info["patch"] = patch_name(sub)
    return info


def record(workdir: str | Path, dest: str | Path, *, super_base: str = "",
           sub_bases: dict[str, str] | None = None) -> dict:
    """Record the superproject and every submodule the build covers (dedicated worktrees and
    shared/symlinked ones alike, i.e. everything `worktree_state` covers) into `dest`."""
    workdir, dest = Path(workdir), Path(dest)
    if dest.is_dir():
        shutil.rmtree(dest)          # a re-record (drift, relaunch) replaces the earlier one
    out: dict = {"super": _record_repo(workdir, dest, "", super_base), "submodules": {}}
    for sub in submodule_paths(workdir):
        path = workdir / sub
        if not (path / ".git").exists():
            continue
        base = (sub_bases or {}).get(sub) or recorded_base(workdir, super_base, sub)
        info = _record_repo(path, dest, sub, base)
        info["shared"] = path.is_symlink()
        out["submodules"][sub] = info
    return out


class Trees:
    """Rebuild `HEAD + patch` states of one repo as git trees in a throwaway object directory
    (the repo's objects are only read, as alternates), and run git commands that see them."""

    def __init__(self, repo: str | Path):
        self.repo = Path(repo)
        self.tmp: Path | None = None
        self.env: dict | None = None
        self._n = 0

    def __enter__(self) -> "Trees":
        common = _out(_git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.tmp = Path(tempfile.mkdtemp(prefix="featx-trees-"))
        (self.tmp / "objects").mkdir()
        self.env = {**os.environ, "GIT_OBJECT_DIRECTORY": str(self.tmp / "objects"),
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(Path(common) / "objects")}
        return self

    def __exit__(self, *exc) -> None:
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def tree(self, commit: str, patch: bytes | None) -> str:
        """`commit` itself when there is no patch, else the tree of commit + patch.
        Raises RuntimeError if the patch does not apply."""
        if not patch:
            return commit
        self._n += 1
        env = {**self.env, "GIT_INDEX_FILE": str(self.tmp / f"index{self._n}")}
        for args, data in ((["read-tree", commit], None),
                           (["apply", "--cached", "--binary", "--whitespace=nowarn", "-"], patch)):
            proc = _git(self.repo, *args, env=env, input=data)
            if proc.returncode != 0:
                raise RuntimeError(f"git {args[0]}: {proc.stderr.decode('utf-8', 'replace').strip()[-300:]}")
        return _out(_git(self.repo, "write-tree", env=env))

