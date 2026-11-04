"""REST app (FastAPI TestClient), CLI (direct mode), MCP tool registration, perf_keys script."""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
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
