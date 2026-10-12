"""Source provenance and binary cache keys.

A worktree's state = HEAD + hash of `git diff HEAD` + hash of untracked (non-ignored)
files, for the superproject and every submodule (shared submodules are symlinks to the
main checkout, so their uncommitted edits are part of what gets built and must be in the
key). bincache key = sha256(state + build recipe).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import config, store

MAX_UNTRACKED_BYTES = 8 << 20
MAX_UNTRACKED_FILES = 2000


def _git(cwd: str | Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", str(cwd), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, check=False)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _git_bytes(cwd, *args) -> bytes:
    proc = subprocess.run(["git", "-C", str(cwd), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, check=False)
    return proc.stdout


def repo_state(path: str | Path, *, ignore_submodules: bool = True) -> dict:
    path = Path(path)
    rc, head = _git(path, "rev-parse", "HEAD")
    if rc != 0:
        return {"path": str(path), "error": "not a git checkout"}
    sub_flag = ["--ignore-submodules=all"] if ignore_submodules else []
    diff = _git_bytes(path, "diff", "HEAD", "--no-ext-diff", "--binary", *sub_flag)
    _, branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    untracked = hashlib.sha256()
    _, names = _git(path, "ls-files", "--others", "--exclude-standard", "-z")
    files = [n for n in names.split("\0") if n][:MAX_UNTRACKED_FILES]
    for name in sorted(files):
        fp = path / name
        untracked.update(name.encode() + b"\0")
        try:
            if fp.is_file() and not fp.is_symlink() and fp.stat().st_size <= MAX_UNTRACKED_BYTES:
                untracked.update(hashlib.sha256(fp.read_bytes()).digest())
        except OSError:
            pass
    return {
        "path": str(path),
        "head": head.strip(),
        "branch": branch.strip(),
        "dirty": bool(diff) or bool(files),
        "diff_sha": hashlib.sha256(diff).hexdigest()[:16] if diff else "",
        "diff_lines": diff.count(b"\n"),
        "untracked_sha": untracked.hexdigest()[:16] if files else "",
        "untracked_files": len(files),
    }


def submodule_paths(worktree: str | Path) -> list[str]:
    rc, out = _git(worktree, "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$")
    if rc != 0:
        return []
    return [line.split(" ", 1)[1] for line in out.splitlines() if " " in line]


def worktree_state(worktree: str | Path) -> dict:
    worktree = Path(worktree)
    state = {"super": repo_state(worktree), "submodules": {}}
    for sub in submodule_paths(worktree):
        sp = worktree / sub
        if (sp / ".git").exists():
            info = repo_state(sp)
            info["shared"] = sp.is_symlink()
            state["submodules"][sub] = info
    return state


def save_patches(worktree: str | Path, dest: Path) -> list[str]:
    """Write the uncommitted diffs (super + submodules) next to the runset so a dirty
    build stays reproducible after the worktree moves on."""
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    diff = _git_bytes(worktree, "diff", "HEAD", "--binary", "--ignore-submodules=all")
    if diff:
        (dest / "super.patch").write_bytes(diff)
        saved.append("super.patch")
    for sub in submodule_paths(worktree):
        sp = Path(worktree) / sub
        if (sp / ".git").exists():
            sdiff = _git_bytes(sp, "diff", "HEAD", "--binary")
            if sdiff:
                name = sub.replace("/", "__") + ".patch"
                (dest / name).write_bytes(sdiff)
                saved.append(name)
    return saved


def _strip_volatile(state: dict) -> dict:
    """Paths and branch names do not affect the binary; heads and diffs do."""
    def clean(info: dict) -> dict:
        return {k: v for k, v in info.items() if k in ("head", "diff_sha", "untracked_sha", "error")}
    return {"super": clean(state.get("super", {})),
            "submodules": {k: clean(v) for k, v in sorted(state.get("submodules", {}).items())}}


def bincache_key(state: dict, build_recipe: dict) -> str:
    recipe = {k: v for k, v in build_recipe.items()
              if k not in ("host", "cores", "mem_gb", "timeout_hours", "workdir", "cache")}
    blob = json.dumps({"src": _strip_volatile(state), "recipe": recipe}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def bincache_entry(key: str) -> Path:
    return config.bincache_dir() / key


def bincache_lookup(key: str) -> Path | None:
    emu = bincache_entry(key) / "emu"
    manifest = bincache_entry(key) / "manifest.json"
    return emu if emu.is_file() and manifest.is_file() else None


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bincache_store(key: str, artifact: Path, manifest: dict) -> Path:
    entry = bincache_entry(key)
    entry.mkdir(parents=True, exist_ok=True)
    tmp = entry / f".emu.{os.getpid()}.tmp"
    shutil.copy2(artifact, tmp)
    os.replace(tmp, entry / "emu")
    manifest = dict(manifest, sha256=file_sha256(entry / "emu"), size=(entry / "emu").stat().st_size,
                    stored=store.now_iso())
    store.write_json(entry / "manifest.json", manifest)
    return entry / "emu"


def import_binary(path: Path, label: str = "") -> tuple[str, Path]:
    """Register an existing emu (e.g. a hand-built 0928 binary) in the bincache; key = file sha."""
    sha = file_sha256(path)
    key = "ext-" + sha[:16]
    if not bincache_lookup(key):
        bincache_store(key, path, {"key": key, "imported_from": str(path), "label": label})
    return key, bincache_entry(key) / "emu"
