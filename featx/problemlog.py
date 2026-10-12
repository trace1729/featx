"""problem-analysis.log: append-only, entries start with the time, then a natural-language
summary of the problem and its solution, then an explanation of how the code change solves
it (the user's mandated format). A JSONL sidecar lets the scheduler check that a design
failure was analysed before it is relaunched."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from . import store

LOG_NAME = "problem-analysis.log"
SIDECAR = ".problem-analysis.jsonl"


def append(target_dir: Path, *, problem: str, solution: str, code: str = "",
           task_id: str | None = None, author: str = "") -> dict:
    problem, solution, code = problem.strip(), solution.strip(), code.strip()
    if not problem or not solution:
        raise ValueError("problem and solution summaries are required")
    when = store.now_iso()
    head = f"## {when}"
    if task_id:
        head += f"  task={task_id}"
    if author:
        head += f"  by={author}"
    body = [head, "", "问题与解决方案总结:", problem, "", solution, ""]
    body += ["代码修改说明:", code or "(无代码修改)", "", ""]
    target_dir.mkdir(parents=True, exist_ok=True)
    with open(target_dir / LOG_NAME, "a", encoding="utf-8") as handle:
        handle.write("\n".join(body))
    rec = {"time": when, "ts": _dt.datetime.now().timestamp(), "task": task_id, "author": author,
           "problem": problem[:500]}
    store.append_jsonl(target_dir / SIDECAR, rec)
    return rec


def entries(target_dir: Path) -> list[dict]:
    return store.read_jsonl(target_dir / SIDECAR)


def analysed_since(target_dir: Path, ts: float, task_id: str | None = None) -> bool:
    for rec in entries(target_dir):
        if rec.get("ts", 0) >= ts and (task_id is None or rec.get("task") in (None, task_id)):
            return True
    return False
