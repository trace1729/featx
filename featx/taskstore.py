"""Read access to tasks/<id>.json. Only the daemon writes these (see sched/scheduler.py);
the CLI reads them directly for tree/mv safety checks when the daemon is down."""

from __future__ import annotations

from pathlib import Path

from . import config, store
from .model import ACTIVE_TASK_STATES, Task


def task_path(task_id: str) -> Path:
    return config.tasks_dir() / f"{task_id}.json"


def load(task_id: str) -> Task | None:
    data = store.read_json(task_path(task_id))
    return Task.model_validate(data) if data else None


def load_all() -> dict[str, Task]:
    out: dict[str, Task] = {}
    tdir = config.tasks_dir()
    if not tdir.is_dir():
        return out
    for path in sorted(tdir.glob("*.json")):
        data = store.read_json(path)
        if data:
            try:
                task = Task.model_validate(data)
            except Exception:
                continue
            out[task.id] = task
    return out


def save(task: Task) -> None:
    store.write_json(task_path(task.id), task.model_dump())


def active_for_nodes(node_ids: set[str]) -> list[Task]:
    return [t for t in load_all().values() if t.node_id in node_ids and t.state in ACTIVE_TASK_STATES]
