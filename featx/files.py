"""Read-only file previews for the UI/CLI, confined to the data root and the worktree root."""

from __future__ import annotations

import os
from pathlib import Path

from . import config

PREVIEW_SUFFIXES = {".csv", ".md", ".json", ".txt", ".log", ".yaml", ".yml", ".sh", ".py", ".tsv", ""}


def allowed_roots() -> list[Path]:
    return [config.root().resolve(), config.wt_root().resolve()]


def resolve(path: str) -> Path:
    """Relative paths are taken from the data root. The fully resolved path (symlinks followed)
    must stay inside an allowed root."""
    p = Path(path)
    if not p.is_absolute():
        p = config.root() / p
    real = p.resolve()
    for r in allowed_roots():
        if real == r or str(real).startswith(str(r) + os.sep):
            return real
    raise PermissionError(f"{path}: outside the featx data/worktree roots")


def read(path: str, *, tail: int | None = None, max_bytes: int = 256 * 1024, skip_perf: bool = False) -> dict:
    real = resolve(path)
    if not real.is_file():
        raise FileNotFoundError(f"{path}: not a file")
    size = real.stat().st_size
    with open(real, "rb") as handle:
        if size > max_bytes:
            # head for csv/json/md (structure lives at the top), tail for logs
            if tail is not None or real.suffix in (".txt", ".log", ""):
                handle.seek(size - max_bytes)
            data = handle.read(max_bytes)
        else:
            data = handle.read()
    text = data.decode(errors="replace")
    truncated = size > max_bytes
    if skip_perf:
        text = "\n".join(l for l in text.splitlines() if not l.startswith("[PERF"))
    if tail is not None:
        text = "\n".join(text.splitlines()[-tail:])
    return {"path": str(real), "size": size, "truncated": truncated, "text": text}


def listing(path: str, *, depth: int = 2, limit: int = 2000) -> dict:
    real = resolve(path)
    if not real.is_dir():
        raise FileNotFoundError(f"{path}: not a directory")
    out = []
    base_depth = len(real.parts)
    for dirpath, dirnames, filenames in os.walk(real):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")) \
            if len(here.parts) - base_depth < depth else []
        for name in sorted(filenames):
            p = here / name
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"path": str(p.relative_to(real)), "size": st.st_size, "mtime": st.st_mtime,
                        "preview": p.suffix in PREVIEW_SUFFIXES})
            if len(out) >= limit:
                return {"root": str(real), "files": out, "truncated": True}
    return {"root": str(real), "files": out, "truncated": False}
