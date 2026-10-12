"""The feature tree: tree/<category>/<feature>/<sub>/node.yaml.

Paths are derived, ids are stable. Every mutation runs under the tree lock so the daemon
and direct CLI calls cannot interleave. Moved/removed paths keep a forwarding symlink, so
absolute paths quoted in old logs, notes and memory files still resolve.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config, store, taskstore
from .model import (
    KINDS,
    NAME_RE,
    NODE_STATUSES,
    RESERVED_NAMES,
    RUNSET_RE,
    VERDICTS,
    Node,
    Runset,
    kind_for_depth,
)


class TreeError(Exception):
    pass


@dataclass
class Entry:
    node: Node
    rel: str                  # "cat/feat/sub"

    @property
    def dir(self) -> Path:
        return config.tree_root() / self.rel

    @property
    def depth(self) -> int:
        return self.rel.count("/") + 1


def _check_rel(rel: str) -> str:
    rel = rel.strip().strip("/")
    if not rel:
        raise TreeError("empty node path")
    parts = rel.split("/")
    for part in parts:
        if not NAME_RE.match(part):
            raise TreeError(f"invalid name {part!r} (allowed: letters, digits, . _ -; must start alnum)")
        if part in RESERVED_NAMES or RUNSET_RE.match(part):
            raise TreeError(f"name {part!r} is reserved (runset-like or a node subdir name)")
    return "/".join(parts)


class Tree:
    def __init__(self) -> None:
        self.by_id: dict[str, Entry] = {}
        self.by_rel: dict[str, Entry] = {}
        self.scan()

    # ---------------------------------------------------------------- read
    def scan(self) -> None:
        self.by_id.clear()
        self.by_rel.clear()
        troot = config.tree_root()
        if not troot.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(troot, followlinks=False):
            here = Path(dirpath)
            # Never descend into runsets / node payload dirs / dot dirs (trash).
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(".") and d not in RESERVED_NAMES and not RUNSET_RE.match(d)
                and not (here / d).is_symlink()
            )
            if here == troot or "node.yaml" not in filenames:
                continue
            data = store.read_yaml(here / "node.yaml")
            if not data:
                continue
            node = Node.model_validate(data)
            rel = here.relative_to(troot).as_posix()
            entry = Entry(node, rel)
            self.by_id[node.id] = entry
            self.by_rel[rel] = entry

    def get(self, ref: str) -> Entry:
        ref = ref.strip()
        rel = ref.strip("/")
        if rel in self.by_rel:
            return self.by_rel[rel]
        if ref in self.by_id:
            return self.by_id[ref]
        # Forwarding symlink left by mv/rm: resolve to the node's new location.
        candidate = config.tree_root() / rel
        if rel and candidate.is_symlink():
            target = candidate.resolve()
            try:
                trel = target.relative_to(config.tree_root().resolve()).as_posix()
            except ValueError:
                trel = None
            if trel in self.by_rel:
                return self.by_rel[trel]
        if len(ref) >= 4:
            hits = [e for i, e in self.by_id.items() if i.startswith(ref.upper())]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise TreeError(f"ambiguous id prefix {ref!r}")
        raise TreeError(f"no such node: {ref}")

    def parent(self, entry: Entry) -> Entry | None:
        return self.by_id.get(entry.node.parent_id) if entry.node.parent_id else None

    def ancestors(self, entry: Entry) -> list[Entry]:
        """Root-first list of ancestors (excluding entry)."""
        chain = []
        cur = self.parent(entry)
        while cur is not None:
            chain.append(cur)
            cur = self.parent(cur)
        return list(reversed(chain))

    def children(self, entry: Entry | None) -> list[Entry]:
        pid = entry.node.id if entry else None
        return sorted((e for e in self.by_id.values() if e.node.parent_id == pid), key=lambda e: e.rel)

    def subtree(self, entry: Entry) -> list[Entry]:
        prefix = entry.rel + "/"
        return [entry] + sorted((e for e in self.by_rel.values() if e.rel.startswith(prefix)), key=lambda e: e.rel)

    def roots(self) -> list[Entry]:
        return self.children(None)

    # ------------------------------------------------------------- runsets
    def runsets(self, entry: Entry) -> list[Runset]:
        out = []
        if not entry.dir.is_dir():
            return out
        for d in sorted(entry.dir.iterdir()):
            if RUNSET_RE.match(d.name) and not d.is_symlink():
                data = store.read_yaml(d / "experiment.yaml")
                if data:
                    out.append(Runset.model_validate(data))
        out.sort(key=lambda r: (r.name.split("-")[0], int(r.name.split("-")[1])))
        return out

    def runset_dir(self, entry: Entry, name: str) -> Path:
        return entry.dir / name

    def find_runset(self, ref: str) -> tuple[Entry, Runset]:
        """ref = '<node path or id>/<YYYYMMDD-n>' or a runset id."""
        if "/" in ref and RUNSET_RE.match(ref.rsplit("/", 1)[1]):
            node_ref, name = ref.rsplit("/", 1)
            entry = self.get(node_ref)
            data = store.read_yaml(entry.dir / name / "experiment.yaml")
            if not data:
                raise TreeError(f"no runset {name} under {entry.rel}")
            return entry, Runset.model_validate(data)
        for entry in self.by_id.values():
            for rs in self.runsets(entry):
                if rs.id == ref or (len(ref) >= 6 and rs.id.startswith(ref.upper())):
                    return entry, rs
        raise TreeError(f"no such runset: {ref}")

    # ------------------------------------------------------------- mutate
    def add(self, rel: str, *, hypothesis: str = "", tags: list[str] | None = None,
            parents: bool = False, kind: str | None = None) -> Entry:
        rel = _check_rel(rel)
        with store.file_lock(config.lock_path()):
            self.scan()
            if rel in self.by_rel:
                raise TreeError(f"node exists: {rel}")
            parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else None
            parent = None
            if parent_rel is not None:
                if parent_rel not in self.by_rel:
                    if not parents:
                        raise TreeError(f"parent {parent_rel} does not exist (use --parents)")
                    self._add_locked(parent_rel, "", [], parents=True, kind=None)
                parent = self.by_rel[parent_rel]
            return self._create(rel, parent, hypothesis, tags or [], kind)

    def _add_locked(self, rel, hypothesis, tags, parents, kind):
        parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else None
        parent = None
        if parent_rel is not None:
            if parent_rel not in self.by_rel:
                self._add_locked(parent_rel, "", [], parents, None)
            parent = self.by_rel[parent_rel]
        return self._create(rel, parent, hypothesis, tags, kind)

    def _create(self, rel, parent, hypothesis, tags, kind) -> Entry:
        depth = rel.count("/") + 1
        kind = kind or kind_for_depth(depth)
        if kind not in KINDS:
            raise TreeError(f"kind must be one of {KINDS}")
        ndir = config.tree_root() / rel
        if ndir.is_symlink():          # stale forwarding link from an earlier mv/rm
            ndir.unlink()
        ndir.mkdir(parents=True, exist_ok=True)
        now = store.now_iso()
        node = Node(id=store.new_id(), kind=kind, name=rel.rsplit("/", 1)[-1],
                    parent_id=parent.node.id if parent else None, hypothesis=hypothesis,
                    tags=tags, created=now, updated=now)
        store.write_yaml(ndir / "node.yaml", node.model_dump())
        entry = Entry(node, rel)
        self.by_id[node.id] = entry
        self.by_rel[rel] = entry
        store.append_jsonl(config.root() / "tree-log.jsonl",
                           {"time": now, "op": "add", "id": node.id, "path": rel})
        return entry

    def save(self, entry: Entry) -> None:
        entry.node.updated = store.now_iso()
        store.write_yaml(entry.dir / "node.yaml", entry.node.model_dump())

    def update(self, ref: str, **fields) -> Entry:
        with store.file_lock(config.lock_path()):
            self.scan()
            entry = self.get(ref)
            node = entry.node
            for key, value in fields.items():
                if value is None:
                    continue
                if key == "verdict" and value not in VERDICTS:
                    raise TreeError(f"verdict must be one of {VERDICTS}")
                if key == "status" and value not in NODE_STATUSES:
                    raise TreeError(f"status must be one of {NODE_STATUSES}")
                if key == "add_evidence":
                    node.evidence = list(dict.fromkeys(node.evidence + list(value)))
                elif key == "add_tags":
                    node.tags = list(dict.fromkeys(node.tags + list(value)))
                elif key == "rm_tags":
                    node.tags = [t for t in node.tags if t not in value]
                elif hasattr(node, key):
                    setattr(node, key, value)
                else:
                    raise TreeError(f"unknown field {key}")
            self.save(entry)
            return entry

    def _active_tasks(self, entries: list[Entry]):
        return taskstore.active_for_nodes({e.node.id for e in entries})

    def mv(self, src_ref: str, dst_rel: str, *, parents: bool = False, rename_branches: bool = True,
           dry_run: bool = False) -> dict:
        """Cascade move: tree dirs, worktrees, branches. Refuses while any task under the
        subtree is queued/running. dry_run runs the same checks and returns the plan only."""
        dst_rel = _check_rel(dst_rel)
        with store.file_lock(config.lock_path()):
            self.scan()
            src = self.get(src_ref)
            if dst_rel == src.rel or dst_rel.startswith(src.rel + "/"):
                raise TreeError("cannot move a node into itself")
            if dst_rel in self.by_rel:
                raise TreeError(f"destination exists: {dst_rel}")
            sub = self.subtree(src)
            active = self._active_tasks(sub)
            if active:
                ids = ", ".join(f"{t.id[:10]}({t.kind}:{t.state})" for t in active)
                raise TreeError(f"refusing to move: active tasks under {src.rel}: {ids}")
            dst_parent_rel = dst_rel.rsplit("/", 1)[0] if "/" in dst_rel else None
            if dst_parent_rel and dst_parent_rel not in self.by_rel and not parents:
                raise TreeError(f"destination parent {dst_parent_rel} does not exist (use --parents)")
            new_dir = config.tree_root() / dst_rel
            if new_dir.exists() and not new_dir.is_symlink():
                raise TreeError(f"destination dir exists on disk: {new_dir}")
            if dry_run:
                return self._mv_plan(src, sub, dst_rel, dst_parent_rel, rename_branches)
            if dst_parent_rel and dst_parent_rel not in self.by_rel:
                self._add_locked(dst_parent_rel, "", [], True, None)
            new_parent = self.by_rel.get(dst_parent_rel) if dst_parent_rel else None

            troot = config.tree_root()
            old_dir, new_dir = troot / src.rel, troot / dst_rel
            if new_dir.is_symlink():
                new_dir.unlink()
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            os.rename(old_dir, new_dir)
            os.symlink(os.path.relpath(new_dir, old_dir.parent), old_dir)

            src_rel = src.rel   # src.rel is rewritten inside the loop; children need the old prefix
            report = {"from": src_rel, "to": dst_rel, "nodes": [], "warnings": []}
            now = store.now_iso()
            for entry in sub:
                rel_new = dst_rel + entry.rel[len(src_rel):]
                node = entry.node
                rec = {"time": now, "from": entry.rel, "to": rel_new}
                if entry is src:
                    node.parent_id = new_parent.node.id if new_parent else None
                    node.name = dst_rel.rsplit("/", 1)[-1]
                    depth = dst_rel.count("/") + 1
                    node.kind = kind_for_depth(depth)
                if node.git:
                    from . import worktree as wt
                    try:
                        rec["git"] = wt.relocate(node, rel_new, rename_branch=rename_branches)
                    except Exception as error:  # keep going: the tree move already happened
                        report["warnings"].append(f"{entry.rel}: worktree relocate failed: {error}")
                node.moves.append(rec)
                entry.rel = rel_new
                store.write_yaml(troot / rel_new / "node.yaml", node.model_dump())
                report["nodes"].append(rec)
            store.append_jsonl(config.root() / "tree-log.jsonl", {"time": now, "op": "mv", **report})
            self.scan()
            return report

    def _mv_plan(self, src: Entry, sub: list[Entry], dst_rel: str, dst_parent_rel: str | None,
                 rename_branches: bool) -> dict:
        from . import worktree as wt
        plan = {"dry_run": True, "from": src.rel, "to": dst_rel, "nodes": [], "create_parents": []}
        if dst_parent_rel and dst_parent_rel not in self.by_rel:
            parts = dst_parent_rel.split("/")
            plan["create_parents"] = [p for p in ("/".join(parts[:i + 1]) for i in range(len(parts)))
                                      if p not in self.by_rel]
        for entry in sub:
            rel_new = dst_rel + entry.rel[len(src.rel):]
            rec = {"from": entry.rel, "to": rel_new,
                   "kind": kind_for_depth(rel_new.count("/") + 1),
                   "runsets": len(self.runsets(entry))}
            if entry.node.git:
                rec["git"] = wt.plan_relocate(entry.node, rel_new, rename_branch=rename_branches)
            plan["nodes"].append(rec)
        return plan

    def rm(self, ref: str, *, force: bool = False, drop_worktrees: bool = False,
           drop_branches: bool = False) -> dict:
        """Soft delete: the subtree moves to tree/.trash/<stamp>-<name> (gc purges it).
        Refuses with active tasks; refuses with runsets or worktrees unless force.
        drop_worktrees also deletes featx branches without own commits; drop_branches all of them."""
        with store.file_lock(config.lock_path()):
            self.scan()
            entry = self.get(ref)
            sub = self.subtree(entry)
            if self._active_tasks(sub):
                raise TreeError("refusing to remove: active tasks in subtree")
            has_runsets = any(self.runsets(e) for e in sub)
            has_wt = [e for e in sub if e.node.git]
            if (has_runsets or has_wt) and not force:
                raise TreeError("subtree has runsets or worktrees; pass --force (data goes to .trash)")
            trash = config.tree_root() / ".trash"
            trash.mkdir(exist_ok=True)
            dst = trash / f"{store.today()}-{store.new_id()[-6:]}-{entry.rel.replace('/', '__')}"
            os.rename(entry.dir, dst)
            report = {"removed": entry.rel, "trash": str(dst), "worktrees": []}
            if drop_worktrees:
                from . import worktree as wt
                for e in has_wt:
                    report["worktrees"].append(wt.remove(e.node, delete_branch=True,
                                                         force_branch=drop_branches))
            store.append_jsonl(config.root() / "tree-log.jsonl",
                               {"time": store.now_iso(), "op": "rm", **report})
            self.scan()
            return report

    def new_runset(self, entry: Entry, *, purpose: str = "", derived_from: str | None = None) -> tuple[Runset, Path]:
        with store.file_lock(config.lock_path()):
            date = store.today()
            n = 1
            for d in entry.dir.iterdir() if entry.dir.is_dir() else []:
                if RUNSET_RE.match(d.name) and d.name.startswith(date + "-"):
                    n = max(n, int(d.name.split("-")[1]) + 1)
            name = f"{date}-{n}"
            rdir = entry.dir / name
            rdir.mkdir(parents=True)
            rs = Runset(id=store.new_id(), name=name, node_id=entry.node.id, purpose=purpose,
                        created=store.now_iso(), derived_from=derived_from)
            store.write_yaml(rdir / "experiment.yaml", rs.model_dump())
            (rdir / "problem-analysis.log").touch()
            return rs, rdir

    def save_runset(self, entry: Entry, rs: Runset) -> None:
        store.write_yaml(entry.dir / rs.name / "experiment.yaml", rs.model_dump())


def run_git(args: list[str], cwd: str | Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, check=False)
    if check and proc.returncode != 0:
        raise TreeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def render_tree(tree: Tree, annotate=None, root: Entry | None = None) -> str:
    """ASCII tree; annotate(entry) -> str appended to each line."""
    lines: list[str] = []

    def walk(entry: Entry, prefix: str, last: bool, top: bool) -> None:
        connector = "" if top else ("└── " if last else "├── ")
        extra = annotate(entry) if annotate else ""
        lines.append(f"{prefix}{connector}{entry.node.name}{extra}")
        kids = tree.children(entry)
        child_prefix = prefix if top else prefix + ("    " if last else "│   ")
        for i, kid in enumerate(kids):
            walk(kid, child_prefix, i == len(kids) - 1, False)

    tops = [root] if root else tree.roots()
    for entry in tops:
        walk(entry, "", True, True)
    return "\n".join(lines)


def purge_trash(apply: bool = False) -> list[tuple[Path, int]]:
    trash = config.tree_root() / ".trash"
    out = []
    if trash.is_dir():
        for d in sorted(trash.iterdir()):
            out.append((d, store.dir_size(d)))
            if apply:
                shutil.rmtree(d, ignore_errors=True)
    return out
