"""REST app (FastAPI TestClient), CLI (direct mode), MCP tool registration, perf_keys script."""

import json
import os
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from featx import cli, service
from featx.post import plugins
from featx.sched import daemon


def test_rest_auth_and_errors(root):
    d = daemon.Daemon(port=1)
    c = TestClient(daemon.build_app(d))
    assert c.get("/health").status_code == 200
    assert c.get("/tree").status_code == 401
    h = {"X-Featx-Token": d.token}
    r = c.post("/nodes", json={"path": "a/b", "parents": True, "hypothesis": "h"}, headers=h)
    assert r.status_code == 200 and r.json()["kind"] == "feature"
    assert "a" in c.get("/tree", params={"fmt": "text"}, headers=h).text
    r = c.post("/nodes", json={"path": "a/b"}, headers=h)
    assert r.status_code == 400 and "exists" in r.json()["error"]
    r = c.post("/tasks", json={"node": "a/b", "kinds": ["run"]}, headers=h)
    assert r.status_code == 400   # no points / no binary
    assert c.get("/events", headers=h).json() == []
    assert c.get("/status", headers=h).json()["tasks"]["queued"] == 0


def test_daemon_stop_interrupts_event_long_poll(root):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["FEATX_URL"] = url
    proc = subprocess.Popen([sys.executable, "-m", "featx.sched.daemon",
                             "--bind", "127.0.0.1", "--port", str(port)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    waiter = stopper = None
    try:
        deadline = time.monotonic() + 15
        while True:
            if proc.poll() is not None:
                raise AssertionError(proc.stdout.read())
            try:
                if httpx.get(url + "/health", timeout=0.2, trust_env=False).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("isolated featxd did not become ready")
            time.sleep(0.1)
        waiter = subprocess.Popen([sys.executable, "-c",
                                   "from featx import client; client.Client().get('/events', "
                                   "unacked=False, wait=3600, timeout=3700)"],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        assert waiter.poll() is None
        stopper = subprocess.Popen([sys.executable, "-m", "featx.cli", "daemon", "stop"],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert proc.wait(timeout=15) == 0
        stdout, stderr = stopper.communicate(timeout=5)
        assert stopper.returncode == 0, stdout + stderr
        assert "stopped" in stdout
        assert json.loads((root / "daemon.json").read_text())["stopped"] > 0
    finally:
        for child in (waiter, stopper, proc):
            if child is not None and child.poll() is None:
                child.kill()
        for child in (waiter, stopper, proc):
            if child is not None:
                child.wait()


def test_submit_dry_run_rest(root, fake_emu):
    from test_sched_local import make_node

    make_node(fake_emu, ["ok_1_0.5"])
    d = daemon.Daemon(port=1)
    c = TestClient(daemon.build_app(d))
    h = {"X-Featx-Token": d.token}
    r = c.post("/tasks", json={"node": "t/sched/x", "kinds": ["run"], "bin": str(fake_emu[0]),
                               "dry_run": True}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] and body["preview_only"]
    assert body["tasks"][0]["points"][0]["name"] == "ok_1_0.5"
    assert c.get("/events", headers=h).json() == []
    assert c.get("/status", headers=h).json()["tasks"]["queued"] == 0


def test_cli_direct(root):
    run = CliRunner().invoke
    r = run(cli.app, ["node", "add", "wpe/real/after", "-p", "-H", "after-anchor pick"])
    assert r.exit_code == 0, r.output
    r = run(cli.app, ["recipe", "set", "wpe/real", "run", "W=1000000", "env.TRACERTL_WP_PICK=after"])
    assert r.exit_code == 0, r.output
    r = run(cli.app, ["recipe", "show", "wpe/real/after", "run"])
    assert "TRACERTL_WP_PICK: after" in r.output and "W: 1000000" in r.output
    r = run(cli.app, ["node", "set", "wpe/real/after", "--verdict", "supported"])
    assert r.exit_code == 0
    r = run(cli.app, ["tree"])
    assert "after" in r.output and "✔" in r.output
    r = run(cli.app, ["node", "mv", "wpe/real/after", "wpe/real/after2"])
    assert r.exit_code == 0, r.output
    r = run(cli.app, ["node", "show", "wpe/real/after2"])
    assert r.exit_code == 0 and "Hypothesis: after-anchor pick" in r.output
    assert "Verdict: supported" in r.output and "Evidence:" in r.output and "Comment:" in r.output
    brief = json.loads(run(cli.app, ["node", "show", "wpe/real/after2", "--json"]).output)
    assert brief["node"]["hypothesis"] == "after-anchor pick" and "recipes" not in brief
    assert {"hypothesis", "verdict", "verdict_note", "evidence", "comment"} <= set(brief["node"])
    r = run(cli.app, ["node", "show", "wpe/real/after2", "--full", "--json"])
    assert json.loads(r.output)["ancestors"][0]["path"] == "wpe"
    r = run(cli.app, ["node", "show", "wpe/real/after2", "--full"])
    assert yaml.safe_load(r.output)["ancestors"][0]["path"] == "wpe"
    r = run(cli.app, ["node", "add", "wpe/run"])
    assert r.exit_code == 1 and "reserved" in r.output
    r = run(cli.app, ["storage"])
    assert r.exit_code == 0, r.output


def test_mcp_tools_registered(root):
    from featx import mcp_server
    import anyio
    tools = anyio.run(mcp_server.server.list_tools)
    names = {t.name for t in tools}
    for want in ("tree_list", "node_context", "node_show", "task_submit", "events_wait", "problem_log_append",
                 "task_relaunch", "result_get", "recipe_set"):
        assert want in names
    assert "(empty tree)" in mcp_server.tree_list()


def test_mcp_node_show_matches_cli_semantics(root):
    from featx import mcp_server
    service.node_add("wpe/feature", hypothesis="try this", parents=True)
    brief = mcp_server.node_show("wpe/feature")
    assert brief["node"]["hypothesis"] == "try this"
    assert "recipes" not in brief
    full = mcp_server.node_show("wpe/feature", full=True)
    assert "recipes" in full and "ancestors" in full


def test_perf_keys_script(tmp_path):
    def point(root, name, c1, i1, c2, i2):
        d = root / name
        d.mkdir(parents=True)
        lines = []
        for t, c, i in ((100, c1, i1), (200, c2, i2)):
            for core in (0, 1):   # two module instances summed (_N normalization)
                mod = f"TOP.SimTop.core_{core}.backend.inner.ctrlBlock.rob"
                lines += [f"[PERF ][time=   {t}] {mod}: clock_cycle, {c // 2}",
                          f"[PERF ][time=   {t}] {mod}: commitInstr, {i // 2}"]
        (d / "simulator_err.txt").write_text("\n".join(lines) + "\n")
    a, b = tmp_path / "a", tmp_path / "b"
    point(a, "x_1_0.5", 10, 10, 1000, 500)
    point(b, "x_1_0.5", 10, 10, 1000, 550)
    pts = tmp_path / "pts.txt"
    pts.write_text("x_1_0.5\nmissing_2_0.1\n")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(plugins.SCRIPTS_DIR / "perf_keys.py"), "--runs", f"ref={a}", f"new={b}",
                    "--points", str(pts), "-o", str(out)], check=True, stdout=subprocess.DEVNULL)
    s = json.loads((out / "summary.json").read_text())
    assert s["common_points"] == 1
    assert abs(s["new.mean_err_pct"] - 10.0) < 1e-6
    assert abs(s["new.rms_err_pct"] - 10.0) < 1e-6 and abs(s["new.ipc_mae"] - 0.05) < 1e-9
    assert abs(s["new.ipc_rmse"] - 0.05) < 1e-9
    assert abs(s["ref.ipc_geomean"] - 0.5) < 1e-9
    assert "missing" in (out / "report.md").read_text()


def test_set_overrides_keep_null():
    # a null deletes the inherited key at submit: it must survive later --set items of its kind
    o = cli._overrides(["run.W=0", "run.coverage=null", "run.exec.diff=/n", "run.exec.gcpt_restorer=/g",
                        "build.cache=false"])
    assert o == {"run": {"W": 0, "coverage": None, "exec": {"diff": "/n", "gcpt_restorer": "/g"}},
                 "build": {"cache": False}}


def test_archive_and_task_state_api(root):
    from featx.model import Task
    from featx.tree import Tree
    d = daemon.Daemon(port=1)
    c = TestClient(daemon.build_app(d))
    h = {"X-Featx-Token": d.token}
    tree = Tree()
    entry = tree.add("api/n", parents=True)
    rs, _ = tree.new_runset(entry)
    ref = f"api/n/{rs.name}"
    r = c.post("/runsets/archive", json={"ref": ref, "note": "duplicate"}, headers=h)
    assert r.status_code == 200 and r.json()["archived"] and r.json()["archive_note"] == "duplicate"
    assert c.get("/runsets", params={"ref": "api/n"}, headers=h).json() == []
    shown = c.get("/runsets", params={"ref": "api/n", "include_archived": True}, headers=h).json()
    assert [x["ref"] for x in shown] == [ref]
    ctx = c.get("/nodes/context", params={"ref": "api/n"}, headers=h).json()
    assert ctx["archived_count"] == 1 and ctx["runsets"] == []

    t = Task(id="TASK1", kind="run", name="run", node_id=entry.node.id, runset_id=rs.id,
             runset_name=rs.name)
    d.sched.tasks[t.id] = t
    assert [x["id"] for x in c.get("/tasks", params={"state": "queued"}, headers=h).json()] == ["TASK1"]
    assert c.get("/tasks", params={"state": "failed"}, headers=h).json() == []
    t.state, t.cancelled_by = "cancelled", "UPSTREAM"
    assert [x["id"] for x in c.get("/tasks", params={"state": "blocked"}, headers=h).json()] == ["TASK1"]


def test_optional_hypothesis_warning_and_result_review_json(root):
    from featx.tree import Tree
    from featx import taskstore
    from featx.model import Task

    run = CliRunner().invoke
    result = run(cli.app, ["node", "add", "review"])
    assert result.exit_code == 0, result.output
    assert "hypothesis is recommended" in result.stderr
    assert Tree().get("review").node.hypothesis == ""
    assert "hypothesis is recommended" not in run(cli.app, ["node", "add", "explained", "-H", "idea"]).stderr
    tree = Tree()
    entry = tree.get("review")
    rs, _ = tree.new_runset(entry)
    task = Task(id="REVIEW1", kind="run", name="run", node_id=entry.node.id,
                runset_id=rs.id, runset_name=rs.name, state="failed")
    rs.tasks = [task.id]
    tree.save_runset(entry, rs)
    taskstore.save(task)
    ref = f"review/{rs.name}"
    result = run(cli.app, ["result", ref])
    assert result.exit_code == 0 and "Review needed" in result.output
    assert f"--evidence {ref}" in result.output
    result = run(cli.app, ["result", ref, "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == service.result_get(ref)
    service.node_update("review", verdict="inconclusive", add_evidence=[ref])
    assert "Review needed" not in run(cli.app, ["result", ref]).output


def test_mcp_research_workflow_and_move_preview(root):
    from featx import mcp_server, taskstore
    from featx.model import Task
    from featx.tree import Tree
    created = mcp_server.node_create("mcp", tags=["old"])
    assert created["warnings"]
    assert "warnings" not in mcp_server.node_create("explained", hypothesis="idea")
    tree = Tree()
    entry = tree.get("mcp")
    rs, _ = tree.new_runset(entry)
    rs.tasks = ["MCP1"]
    tree.save_runset(entry, rs)
    taskstore.save(Task(id="MCP1", kind="run", name="run", node_id=entry.node.id,
                        runset_id=rs.id, runset_name=rs.name, state="failed"))
    ref = f"mcp/{rs.name}"
    assert mcp_server.result_get(ref)["review"]["needed"]
    assert mcp_server.node_show("mcp")["runsets"][0]["review"]["needed"]
    assert mcp_server.node_show("mcp", runsets=0)["runsets"] == []
    assert tree.get("mcp").node.verdict == "open"
    assert tree.get("mcp").node.evidence == []
    mcp_server.node_update("mcp", verdict="inconclusive", add_evidence=[ref], rm_tags=["old"])
    assert not mcp_server.result_get(ref)["review"]["needed"]
    assert Tree().get("mcp").node.tags == []
    mcp_server.node_move("mcp", "renamed", dry_run=True, keep_branches=True)
    assert Tree().get("mcp").node.id == entry.node.id


def test_rest_task_detail_summary_and_jobs(root):
    """Rest stays full by default (the UI renders every job); CLI/MCP ask for summary/jobs."""
    from featx.model import Job, Task

    d = daemon.Daemon(port=1)
    c = TestClient(daemon.build_app(d))
    h = {"X-Featx-Token": d.token}
    e = service.node_add("t/rest/x", parents=True)
    jobs = [Job(name=f"bench_{i}", rel=f"run/b{i}", state="running", host="worker-a") for i in range(60)]
    jobs += [Job(name=f"bad_{i}", rel=f"run/f{i}", state="failed", rc=1) for i in range(3)]
    d.sched.tasks["T1"] = Task(id="T1", kind="run", name="run", node_id=e["id"], runset_id="R1",
                               runset_name="20261104-1", spec={"I": 1000000}, jobs=jobs)

    full = c.get("/tasks/T1", headers=h).json()
    assert len(full["jobs"]) == 63 and "jobs_total" not in full          # UI contract unchanged

    summary = c.get("/tasks/T1", params={"detail": "summary"}, headers=h).json()
    assert "jobs" not in summary and summary["jobs_total"] == 63
    assert [j["name"] for j in summary["jobs_failed"]] == ["bad_0", "bad_1", "bad_2"]
    assert summary["jobs_truncated"] is False and "--jobs" in summary["jobs_hint"]

    page = c.get("/tasks/T1", params={"detail": "jobs", "state": "failed", "limit": 1}, headers=h).json()
    assert page["jobs_total"] == 3 and page["jobs_truncated"] is True
    assert [j["name"] for j in page["jobs"]] == ["bad_0"]

    bad = c.get("/tasks/T1", params={"detail": "everything"}, headers=h)
    assert bad.status_code == 400 and "detail must be" in bad.json()["error"]


def test_rest_result_detail_keeps_full_default_for_the_ui(root):
    from featx.tree import Tree

    d = daemon.Daemon(port=1)
    c = TestClient(daemon.build_app(d))
    h = {"X-Featx-Token": d.token}
    e = service.node_add("r/api", parents=True)
    tree = Tree()
    entry = tree.get("r/api")
    rs, _ = tree.new_runset(entry, purpose="p")
    rs.recipes = {"run": {"W": 1}}
    rs.provenance = {"super": {"head": "a"}, "submodules": {"s": {"dirty": True}}}
    tree.save_runset(entry, rs)
    ref = f"{e['path']}/{rs.name}"

    full = c.get("/result", params={"ref": ref}, headers=h).json()
    assert full["recipes"] == {"run": {"W": 1}} and full["provenance"]["submodules"]["s"]["dirty"]

    short = c.get("/result", params={"ref": ref, "detail": "summary"}, headers=h).json()
    assert "recipes" not in short and short["provenance"]["submodules"] == 1
    assert short["provenance"]["submodules_dirty"] == ["s"]

    added = c.get("/result", params={"ref": ref, "detail": "summary", "sections": "recipes"},
                  headers=h).json()
    assert added["recipes"] == {"run": {"W": 1}} and "sections_hint" in added
    assert c.get("/result", params={"ref": ref, "detail": "nope"}, headers=h).status_code == 400


def test_rest_node_context_keeps_full_default_and_accepts_detail(root):
    from featx.tree import Tree

    d = daemon.Daemon(port=1)
    c = TestClient(daemon.build_app(d))
    h = {"X-Featx-Token": d.token}
    service.node_add("n/ctx", parents=True)
    tree = Tree()
    entry = tree.get("n/ctx")
    rs, _ = tree.new_runset(entry, purpose="p")
    rs.recipes = {"run": {"W": 1}}
    rs.overrides = {"run": {"W": 1}}
    tree.save_runset(entry, rs)

    full = c.get("/nodes/context", params={"ref": "n/ctx"}, headers=h).json()   # UI contract
    short = c.get("/nodes/context", params={"ref": "n/ctx", "detail": "summary"}, headers=h).json()
    assert "params" in full["runsets"][-1] and "overrides" in full["runsets"][-1]
    assert "params" not in short["runsets"][-1] and "overrides" not in short["runsets"][-1]
    assert short["runsets"][-1]["purpose"] == "p" and short["recipes"] == full["recipes"]


def _daemon_info(monkeypatch, **kw):
    info = {"host": "localhost", "port": 1, "pid": 4242, "heartbeat": time.time(), **kw}
    monkeypatch.setattr(cli, "daemon_info", lambda: dict(info))
    monkeypatch.setattr(cli, "daemon_alive", lambda i, age=30: True)
    monkeypatch.setattr(cli, "time", SimpleNamespace(sleep=lambda s: None, time=time.time))
    return info


def test_daemon_stop_escalates_to_kill_when_term_is_ignored(root, monkeypatch):
    from featx.sched import executor

    _daemon_info(monkeypatch)
    kills = []
    monkeypatch.setattr(executor, "kill", lambda host, pid, sig="TERM": kills.append(sig))
    monkeypatch.setattr(executor, "alive_on_host", lambda host, pid: len(kills) < 2)  # dies on KILL

    r = CliRunner().invoke(cli.app, ["daemon", "stop"])
    assert r.exit_code == 0, r.output
    assert kills == ["TERM", "KILL"] and "sending KILL" in r.output
    assert "stopped" in r.output


def test_daemon_stop_does_not_kill_a_responsive_daemon(root, monkeypatch):
    from featx.sched import executor

    _daemon_info(monkeypatch)
    kills = []
    monkeypatch.setattr(executor, "kill", lambda host, pid, sig="TERM": kills.append(sig))
    monkeypatch.setattr(executor, "alive_on_host", lambda host, pid: False)

    r = CliRunner().invoke(cli.app, ["daemon", "stop"])
    assert r.exit_code == 0 and kills == ["TERM"] and "sending KILL" not in r.output


def test_daemon_start_warns_when_the_api_is_unreachable(root, monkeypatch):
    _daemon_info(monkeypatch)

    def boom(*_a, **_kw):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(cli, "client", lambda: SimpleNamespace(get=boom))

    r = CliRunner().invoke(cli.app, ["daemon", "start"])
    assert r.exit_code == 0 and "half-stopped" in r.output and "already running" in r.output


def test_shutdown_watchdog_leaves_after_the_grace_period(root, monkeypatch):
    from featx.sched import daemon

    exits = []
    monkeypatch.setattr(daemon.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(daemon, "time", SimpleNamespace(sleep=lambda s: None, time=time.time))
    daemon._watch_shutdown(SimpleNamespace(should_exit=True), delay=0)
    assert exits == [0]


def test_term_handler_arms_a_hard_exit(root, monkeypatch):
    from featx.sched import daemon

    armed = []

    class Timer:
        def __init__(self, delay, fn, args=()):
            self.delay, self.fn, self.args, self.daemon = delay, fn, args, False
            armed.append(self)

        def start(self):
            pass

    monkeypatch.setattr(daemon.threading, "Timer", Timer)
    with pytest.raises(SystemExit):
        daemon._exit_on_term(15, None)
    assert armed[0].delay == daemon.HARD_EXIT_AFTER and armed[0].fn is daemon.os._exit
    assert armed[0].args == (0,) and armed[0].daemon is True   # must not delay a clean exit
