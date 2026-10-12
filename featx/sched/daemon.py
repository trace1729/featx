"""featxd: FastAPI REST server + scheduler tick thread. One instance per data root
(guarded by $FEATX_ROOT/.daemon.lock); discovered through $FEATX_ROOT/daemon.json.

Start under runwatch on the configured host:
  RUNWATCH_DIR=$FEATX_ROOT/.runwatch runwatch.sh start featxd -- <venv>/bin/featxd
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import secrets
import signal
import socket
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from .. import config, service, store
from ..events import Inbox
from ..tree import Tree, TreeError
from .classify import tail_text
from .scheduler import Scheduler, SchedError

try:
    from fastapi import Body, FastAPI, Request
    from fastapi.responses import JSONResponse, PlainTextResponse
except ImportError:  # pragma: no cover - nodes without the venv never import the daemon
    FastAPI = None


def ensure_token() -> str:
    path = config.token_path()
    if path.is_file():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_hex(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(tok + "\n")
    return tok


class Daemon:
    def __init__(self, port: int | None = None, bind: str | None = None) -> None:
        s = config.settings()["daemon"]
        self.port = port or int(s["port"])
        self.bind = bind or s["bind"]
        self.tick_seconds = float(s["tick_seconds"])
        config.ensure_layout()
        self.inbox = Inbox()
        self.sched = Scheduler(self.inbox)
        self.token = ensure_token()
        self.stop = threading.Event()
        self.started = store.now_iso()

    def write_daemon_json(self) -> None:
        store.write_json(config.daemon_json(), {
            "host": socket.gethostname().split(".")[0], "port": self.port, "pid": os.getpid(),
            "started": self.started, "heartbeat": time.time(), "root": str(config.root())})

    def loop(self) -> None:
        while not self.stop.is_set():
            t0 = time.time()
            try:
                self.sched.tick()
            except Exception:  # noqa: BLE001
                self.sched.tick_errors = (self.sched.tick_errors + [traceback.format_exc()])[-20:]
            try:
                self.write_daemon_json()
            except OSError:
                pass
            self.stop.wait(max(0.5, self.tick_seconds - (time.time() - t0)))


def build_app(d: Daemon) -> "FastAPI":
    app = FastAPI(title="featx", version="0.1")
    sched = d.sched

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if request.url.path not in ("/health",) and request.headers.get("x-featx-token") != d.token:
            return JSONResponse({"error": "bad or missing X-Featx-Token"}, status_code=401)
        return await call_next(request)

    async def user_error(_request: Request, error: Exception):
        return JSONResponse({"error": str(error), "type": type(error).__name__}, status_code=400)

    for exc in (TreeError, SchedError, KeyError, ValueError, FileNotFoundError, TimeoutError):
        app.add_exception_handler(exc, user_error)

    def locked(fn, *a, **kw):
        with sched.lock:
            return fn(*a, **kw)

    # ------------------------------------------------------------- misc
    @app.get("/health")
    def health():
        return {"ok": True, "host": socket.gethostname().split(".")[0], "pid": os.getpid(),
                "started": d.started, "last_tick": sched.last_tick, "root": str(config.root())}

    @app.get("/status")
    def status():
        tasks = list(sched.tasks.values())
        return {"started": d.started, "last_tick": sched.last_tick, "tick_errors": sched.tick_errors[-5:],
                "tasks": {s: sum(1 for t in tasks if t.state == s) for s in
                          ("queued", "running", "succeeded", "failed", "cancelled")},
                "jobs_per_host": sched.jobs_per_host(),
                "unacked_events": len(d.inbox.query(unacked=True, limit=100000))}

    @app.get("/hosts")
    def hosts(refresh: bool = False):
        sched.placement.refresh(force=refresh)
        return sched.placement.snapshot(sched.jobs_per_host())

    # ------------------------------------------------------------- tree
    @app.get("/tree")
    def tree(fmt: str = "rows", root: str | None = None):
        t = Tree()
        st = sched.node_status()
        if fmt == "text":
            return PlainTextResponse(service.tree_text(t, st, root))
        return service.tree_rows(t, st)

    @app.post("/nodes")
    def node_add(body: dict = Body(...)):
        return locked(service.node_add, body["path"], hypothesis=body.get("hypothesis", ""),
                      tags=body.get("tags"), parents=body.get("parents", False),
                      worktree_create=body.get("worktree", False), base=body.get("base"))

    @app.get("/nodes/context")
    def node_context(ref: str, max_runsets: int = 3):
        return service.node_context(ref, max_runsets)

    @app.post("/nodes/update")
    def node_update(body: dict = Body(...)):
        ref = body.pop("ref")
        return locked(service.node_update, ref, **body)

    @app.post("/nodes/mv")
    def node_mv(body: dict = Body(...)):
        return locked(service.node_mv, body["src"], body["dst"], parents=body.get("parents", False),
                      rename_branches=body.get("rename_branches", True))

    @app.post("/nodes/rm")
    def node_rm(body: dict = Body(...)):
        return locked(service.node_rm, body["ref"], force=body.get("force", False),
                      drop_worktrees=body.get("drop_worktrees", False),
                      drop_branches=body.get("drop_branches", False))

    @app.get("/runsets")
    def runsets(ref: str):
        return service.runsets(ref)

    @app.get("/result")
    def result(ref: str):
        return service.result_get(ref)

    # ---------------------------------------------------------- recipes
    @app.get("/recipes")
    def recipe_get(ref: str, kind: str, effective: bool = True, explain: bool = False):
        out = service.recipe_get(ref, kind, effective=effective, explain=explain)
        return PlainTextResponse(out) if explain else out

    @app.post("/recipes")
    def recipe_set(body: dict = Body(...)):
        return locked(service.recipe_set, body["ref"], body["kind"], body["data"], merge=body.get("merge", False))

    @app.post("/scripts")
    def script_write(body: dict = Body(...)):
        return service.script_write(body["ref"], body["name"], body["content"], body.get("executable", True))

    @app.get("/scripts")
    def script_read(ref: str, name: str):
        return PlainTextResponse(service.script_read(ref, name))

    @app.post("/worktrees")
    def worktree_create(body: dict = Body(...)):
        ref = body.pop("ref")
        return locked(service.worktree_create, ref, **body)

    @app.get("/worktrees/status")
    def worktree_status(ref: str):
        return service.worktree_status(ref)

    # ------------------------------------------------------------ tasks
    @app.post("/tasks")
    def submit(body: dict = Body(...)):
        return sched.submit(body["node"], body.get("kinds") or ["build", "run", "post"],
                            runset=body.get("runset"), purpose=body.get("purpose", ""),
                            overrides=body.get("overrides"), bin_ref=body.get("bin"),
                            after=body.get("after"), derived_from=body.get("derived_from"),
                            only_points=body.get("points"), post_steps=body.get("post_steps"))

    @app.get("/tasks")
    def tasks(active: bool = False, node: str | None = None, limit: int = 200):
        with sched.lock:
            return sched.list_tasks(active_only=active, node_prefix=node, limit=limit)

    @app.get("/tasks/{tid}")
    def task(tid: str, spec: bool = False):
        with sched.lock:
            t = sched.get(tid)
            out = sched.task_view(t, jobs=True)
            if spec:
                out["spec"] = t.spec
            return out

    @app.get("/tasks/{tid}/log")
    def task_log(tid: str, job: str | None = None, stream: str = "out", lines: int = 120):
        with sched.lock:
            path = sched.log_path(tid, job, stream)
        text = tail_text(path, 512 * 1024, skip_perf=True) if path.exists() else ""
        return {"path": str(path), "exists": path.exists(),
                "tail": "\n".join(text.splitlines()[-lines:])}

    @app.post("/tasks/{tid}/relaunch")
    def relaunch(tid: str, body: dict = Body(default={})):
        return sched.relaunch(tid, jobs=body.get("jobs"), force=body.get("force", False),
                              reason=body.get("reason", ""))

    @app.post("/tasks/{tid}/resubmit")
    def resubmit(tid: str, body: dict = Body(default={})):
        return sched.resubmit(tid, only_failed=body.get("only_failed", True), force=body.get("force", False),
                              overrides=body.get("overrides"), kinds=body.get("kinds"),
                              purpose=body.get("purpose", ""), bin_ref=body.get("bin"))

    @app.post("/tasks/{tid}/cancel")
    def cancel(tid: str, body: dict = Body(default={})):
        return sched.cancel(tid, jobs=body.get("jobs"))

    @app.post("/tasks/{tid}/unblock")
    def unblock(tid: str, body: dict = Body(default={})):
        return sched.unblock(tid, int(body.get("extra", 1)))

    @app.get("/eta")
    def eta_view():
        with sched.lock:
            return [{"id": v["id"], "node": v["node"], "runset": v["runset"], "kind": v["kind"],
                     "state": v["state"], "eta_s": v["eta_s"], "progress_pct": v.get("progress_pct"),
                     "counts": v["counts"]} for v in sched.list_tasks(active_only=True)]

    # ----------------------------------------------------------- events
    @app.get("/events")
    def events(since: int = 0, unacked: bool = True, types: str | None = None, node: str | None = None,
               limit: int = 100, wait: float = 0.0):
        q: dict[str, Any] = {"since": since, "unacked": unacked, "types": types.split(",") if types else None,
                             "node_prefix": node, "limit": limit}
        if wait > 0:
            return d.inbox.wait(min(wait, 3600), **q)
        return d.inbox.query(**q)

    @app.post("/events/ack")
    def ack(body: dict = Body(...)):
        return {"acked": d.inbox.ack(body.get("seqs"), body.get("upto"))}

    @app.post("/problem-log")
    def plog(body: dict = Body(...)):
        return service.problem_log_append(body["target"], problem=body["problem"], solution=body["solution"],
                                          code=body.get("code", ""), task_id=body.get("task"),
                                          author=body.get("author", ""))

    # --------------------------------------------------------- storage
    @app.get("/storage")
    def storage(worktrees: bool = False):
        return service.storage_report(worktrees=worktrees)

    @app.post("/gc")
    def gc(body: dict = Body(default={})):
        return locked(service.gc, apply=body.get("apply", False), build_dirs=body.get("build_dirs", False))

    @app.get("/baselines")
    def baselines_list():
        return service.baselines_list()

    @app.post("/baselines")
    def baseline_add(body: dict = Body(...)):
        return service.baseline_add(body["name"], body["root"], body.get("description", ""),
                                    binary=body.get("binary"), collected=body.get("collected"))

    @app.post("/bins/import")
    def bin_import(body: dict = Body(...)):
        return service.bin_import(body["path"], body.get("label", ""))

    return app


@contextlib.contextmanager
def single_instance():
    path = config.root() / ".daemon.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        info = store.read_json(config.daemon_json(), {})
        raise SystemExit(f"featxd already running for {config.root()} ({info.get('host')}:{info.get('port')} "
                         f"pid {info.get('pid')})")
    try:
        yield
    finally:
        handle.close()


def _exit_on_term(_signum, _frame) -> None:
    raise SystemExit(0)


def main() -> None:
    ap = argparse.ArgumentParser(prog="featxd")
    ap.add_argument("--port", type=int)
    ap.add_argument("--bind")
    args = ap.parse_args()
    import uvicorn
    with single_instance():
        d = Daemon(args.port, args.bind)
        d.write_daemon_json()
        th = threading.Thread(target=d.loop, name="featx-tick", daemon=True)
        th.start()
        # uvicorn restores the previous SIGTERM handler after its graceful shutdown and then
        # re-raises the signal; with the default handler that kills us before `finally` runs.
        signal.signal(signal.SIGTERM, _exit_on_term)
        try:
            uvicorn.run(build_app(d), host=d.bind, port=d.port, log_level="warning")
        finally:
            d.stop.set()
            th.join(timeout=30)
            info = store.read_json(config.daemon_json(), {})
            if info.get("pid") == os.getpid():
                info["stopped"] = time.time()
                store.write_json(config.daemon_json(), info)


if __name__ == "__main__":
    main()
