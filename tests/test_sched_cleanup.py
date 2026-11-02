"""Dependency-failure cleanup, orphan hardening, reap and task state filters (plan 1.1-1.7)."""

import subprocess
import time
from pathlib import Path

from featx import config, service, store
from featx.events import Inbox
from featx.sched import executor
from featx.sched.scheduler import Scheduler
from featx.tree import Tree

from test_sched_local import LocalPlacement, make_node, run_until, sched


class FakeClient:
    def __init__(self, views):
        self.views = views

    def get(self, path, **kw):
        if path == "/events":
            return []
        return self.views[path.rsplit("/", 1)[-1]]


def test_dependency_failure_cancels_and_relaunch_revives(root, fake_emu, tmp_path):
    make_node(fake_emu, ["ok_1_0.5"])
    wd = tmp_path / "wd"
    wd.mkdir()
    service.recipe_set("t/sched/x", "build", {"type": "script", "workdir": str(wd), "cores": 1,
                                              "mem_gb": 1, "script": "echo boom >&2; exit 1"})
    s = sched()
    res = s.submit("t/sched/x", ["build", "run", "post"])
    b, r, p = (t["id"] for t in res["tasks"])
    run_until(s, lambda: s.tasks[b].state == "failed")

    s.tick()                      # run sees a failed dependency -> cancelled, not stuck queued
    assert s.tasks[r].state == "cancelled" and s.tasks[r].cancelled_by == b
    assert s.tasks[r].note == f"dependency build {b[:12]} failed"
    s.tick()                      # and the cascade reaches post on the next tick
    assert s.tasks[p].state == "cancelled" and s.tasks[p].cancelled_by == r

    events = [e for e in s.inbox.query(types=["cancelled"]) if e.get("reason") == "dependency"]
    assert {(e["task"], e["dependency"]) for e in events} == {(r, b), (p, r)}

    # A dependent a human cancelled keeps its state through a relaunch.
    s.tasks[p].cancelled_by = None
    s.tasks[p].note = "cancelled"
    service.problem_log_append(res["runset"], problem="build script broken", solution="fixed it")
    out = s.relaunch(b)
    assert out["revived"] == [r]
    assert s.tasks[r].state == "queued" and s.tasks[r].cancelled_by is None
    assert s.tasks[p].state == "cancelled"

    # The relaunched build fails again: the revived dependent is cancelled again ...
    run_until(s, lambda: s.tasks[b].state == "failed")
    s.tick()
    assert s.tasks[r].state == "cancelled" and s.tasks[r].cancelled_by == b
    # ... and a human-cancelled one is left alone.
    assert s.tasks[p].state == "cancelled"


def test_cancel_is_transitive_and_relaunch_revives_the_cascade(root, fake_emu):
    make_node(fake_emu, ["ok_1_0.5"])
    s = sched()
    res = s.submit("t/sched/x", ["run", "post"], bin_ref=str(fake_emu[0]))
    r, p = (t["id"] for t in res["tasks"])
    out = s.cancel(r)             # post is only a transitive dependent
    assert out["dependents"] == [p]
    assert s.tasks[p].state == "cancelled" and s.tasks[p].cancelled_by == r
    revived = s.relaunch(r)
    assert revived["revived"] == [p]
    assert s.tasks[r].state == "queued" and s.tasks[p].state == "queued"
    assert s.tasks[p].cancelled_by is None


def test_wait_reports_dependency_cancelled_tasks_as_blocked(root):
    from featx.waiting import wait_tasks
    views = {
        "B": {"id": "B", "state": "failed", "after": []},
        "R": {"id": "R", "state": "cancelled", "cancelled_by": "B", "after": ["B"]},
        "P": {"id": "P", "state": "cancelled", "cancelled_by": "R", "after": ["R"]},
    }
    out = wait_tasks(FakeClient(views), ["R", "P"], timeout=0)
    assert out["blocked"]["R"]["failed_dependencies"] == ["B"]
    assert out["blocked"]["P"]["failed_dependencies"] == ["R"]
    assert out["finished"] == {} and out["pending"] == []


def test_late_launch_after_reported_failure_is_killed(root, fake_emu, monkeypatch):
    make_node(fake_emu, ["ok_1_0.5"])
    s = sched()
    res = s.submit("t/sched/x", ["run"], bin_ref=str(fake_emu[0]))
    tid = res["tasks"][0]["id"]
    monkeypatch.setattr(executor, "launch", lambda *a, **kw: (False, "featx: launch failed: ssh timeout"))
    run_until(s, lambda: s.tasks[tid].state == "failed")
    rdir = Tree().find_runset(res["runset"])
    jdir = rdir[0].dir / rdir[1].name / "run" / "ok_1_0.5"
    attempts = {o["attempt"] for o in s._orphans}
    assert attempts == {1, 2}      # the original attempt and the infra retry are both watched

    # The "failed" launch starts late and writes its pid: the orphan watcher kills it.
    proc = subprocess.Popen(["sleep", "300"])
    try:
        fx = jdir / ".fx" / str(max(attempts))
        fx.mkdir(parents=True, exist_ok=True)
        (fx / "pid").write_text(f"{proc.pid}\n")
        deadline = time.time() + 20
        while proc.poll() is None and time.time() < deadline:
            s.tick()
            time.sleep(0.1)
        assert proc.poll() is not None, "the late-starting attempt was not killed"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_pending_kills_and_orphans_survive_a_restart(root):
    s = sched()
    s._queue_kill("localhost", 4242)
    s._register_orphan("localhost", Path("/tmp/x"), 3)
    blob = store.read_json(config.root() / "sched-pending.json", {})
    assert blob["kills"][0]["pid"] == 4242
    assert blob["orphans"][0] == {"host": "localhost", "job_dir": "/tmp/x", "attempt": 3,
                                  "deadline": blob["orphans"][0]["deadline"]}

    again = Scheduler(Inbox(), placement=LocalPlacement())
    assert [(k["host"], k["pid"], k["signal"]) for k in again._kills] == [("localhost", 4242, "TERM")]
    assert [(o["host"], str(o["job_dir"]), o["attempt"]) for o in again._orphans] == [("localhost", "/tmp/x", 3)]
    assert again._kill_seq == s._kill_seq


def test_kill_to_an_unreachable_host_is_retried_until_the_window_ends(root, monkeypatch):
    s = sched()
    s._queue_kill("localhost", 4242)
    calls = []
    monkeypatch.setattr(executor, "kill_many",
                        lambda host, pids, signal="TERM": calls.append(list(pids)) or False)
    s._do_ssh_work()
    assert calls == [[4242]] and len(s._kills) == 1      # kept for the next tick
    s._kills[0]["queued"] = 0                            # older than liveness.kill_retry_hours
    s._do_ssh_work()
    assert s._kills == []


def test_jobs_of_a_blocked_task_are_not_launched(root, fake_emu, monkeypatch):
    make_node(fake_emu, ["ok_1_0.5"])
    s = sched()
    res = s.submit("t/sched/x", ["run"], bin_ref=str(fake_emu[0]))
    task = s.tasks[res["tasks"][0]["id"]]
    with s.lock:
        s._materialize(s._tree(), task)
    calls = []
    monkeypatch.setattr(executor, "launch", lambda *a, **kw: calls.append(a) or (True, ""))
    task.blocked = True
    s.tick()
    assert calls == [] and task.jobs[0].state == "queued"
    task.blocked = False
    s.tick()
    assert len(calls) == 1


def test_jobs_of_a_blocked_task_are_still_polled(root, fake_emu):
    make_node(fake_emu, ["hang_1_0.5"])
    s = sched()
    res = s.submit("t/sched/x", ["run"], bin_ref=str(fake_emu[0]))
    task = s.tasks[res["tasks"][0]["id"]]
    run_until(s, lambda: task.jobs[0].state == "running")
    task.blocked = True
    seen = task.jobs[0].hb_seen
    # a blocked task is not launched but its running jobs keep being polled
    run_until(s, lambda: (task.jobs[0].hb_seen or 0) > (seen or 0), timeout=20)
    s.cancel(task.id)


def test_reap_finds_and_kills_a_stale_exec(root, tmp_path):
    class ReapPlacement(LocalPlacement):
        def snapshot(self, jobs_per_host):
            return [{"host": "localhost"}]

    s = Scheduler(Inbox(), placement=ReapPlacement())
    fake = tmp_path / "featx-exec"
    fake.write_text("#!/usr/bin/env bash\nsleep 300\n")
    fake.chmod(0o755)
    jdir = config.root() / "tree" / "stale" / "20260101-1" / "run" / "p"
    jdir.mkdir(parents=True)
    proc = subprocess.Popen(["bash", str(fake), str(jdir), "4"])
    try:
        deadline = time.time() + 20
        rows = []
        while not rows and time.time() < deadline:
            rows = [r for r in s.reap(apply=False)["processes"] if r.get("pid") == proc.pid]
            time.sleep(0.1)
        assert rows and rows[0]["reason"] == "job not found"
        assert proc.poll() is None                       # dry run kills nothing
        out = s.reap(apply=True)
        assert out["killed"] >= 1
        proc.wait(timeout=10)
        assert proc.returncode is not None
        assert s.inbox.query(types=["orphan"])
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_list_tasks_filters_by_state(root, fake_emu):
    make_node(fake_emu, ["ok_1_0.5"])
    s = sched()
    res = s.submit("t/sched/x", ["run"], bin_ref=str(fake_emu[0]))
    task = s.tasks[res["tasks"][0]["id"]]
    with s.lock:
        s._materialize(s._tree(), task)
    assert [r["id"] for r in s.list_tasks(states=["running"])] == [task.id]
    task.blocked = True
    assert [r["id"] for r in s.list_tasks(states=["blocked"])] == [task.id]
    task.blocked = False
    task.state, task.cancelled_by = "cancelled", "upstream"
    assert [r["id"] for r in s.list_tasks(states=["blocked"])] == [task.id]
    assert s.list_tasks(states=["failed"]) == []
    assert [r["id"] for r in s.list_tasks(states=["cancelled", "failed"])] == [task.id]
