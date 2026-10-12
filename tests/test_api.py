"""REST app (FastAPI TestClient), CLI (direct mode), MCP tool registration, perf_keys script."""

import json
import subprocess
import sys

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
    assert json.loads(r.output)["ancestors"][0]["path"] == "wpe"
    r = run(cli.app, ["node", "add", "wpe/run"])
    assert r.exit_code == 1 and "reserved" in r.output
    r = run(cli.app, ["storage"])
    assert r.exit_code == 0, r.output


def test_mcp_tools_registered(root):
    from featx import mcp_server
    import anyio
    tools = anyio.run(mcp_server.server.list_tools)
    names = {t.name for t in tools}
    for want in ("tree_list", "node_context", "task_submit", "events_wait", "problem_log_append",
                 "task_relaunch", "result_get", "recipe_set"):
        assert want in names
    assert "(empty tree)" in mcp_server.tree_list()


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
    assert abs(s["ref.ipc_geomean"] - 0.5) < 1e-9
    assert "missing" in (out / "report.md").read_text()
