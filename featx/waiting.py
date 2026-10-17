"""Task waiting shared by the CLI and MCP; dependency events wake long polls."""

import time

from .model import TERMINAL_TASK_STATES


def wait_tasks(client, tasks: list[str], timeout: float = 86400) -> dict:
    pending = set(tasks)
    finished: dict[str, dict] = {}
    blocked: dict[str, dict] = {}
    since = 0
    deadline = time.monotonic() + max(0.0, timeout)
    while pending:
        views: dict[str, dict] = {}

        def view(tid):
            if tid not in views:
                views[tid] = client.get(f"/tasks/{tid}", timeout=30)
            return views[tid]

        def failed_dependencies(tid, seen):
            if tid in seen:
                return []
            seen = seen | {tid}
            current = view(tid)
            if current["state"] in ("failed", "cancelled"):
                return [current.get("id", tid)]
            if current["state"] == "succeeded":
                return []
            return [failed for dep in current.get("after", [])
                    for failed in failed_dependencies(dep, seen)]

        for tid in list(pending):
            current = view(tid)
            if current["state"] in TERMINAL_TASK_STATES:
                finished[tid] = current
                pending.remove(tid)
            elif current["state"] == "queued":
                failures = failed_dependencies(tid, set())
                if failures:
                    blocked[tid] = {**current, "failed_dependencies": sorted(set(failures))}
                    pending.remove(tid)
        left = deadline - time.monotonic()
        if not pending or left <= 0:
            break
        # Include upstream tasks: a build failure must wake a wait on run/post alone.
        watched = {v.get("id", tid) for tid, v in views.items()}
        duration = min(left, 3600)
        events = client.get("/events", since=since, unacked=False,
                            task=",".join(sorted(watched)),
                            types="task_done,failure,cancelled,relaunched", wait=duration,
                            limit=100, timeout=duration + 30)
        if events:
            since = max(since, max(int(e["seq"]) for e in events))
    return {"finished": finished, "blocked": blocked, "pending": sorted(pending),
            "timed_out": bool(pending)}
