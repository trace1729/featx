"""UI backend: leaderboard, config editing, file previews, mv dry run, static UI + auth."""

import json
import os
import subprocess

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from featx import cli, config, files, leaderboard, store, taskstore
from featx.model import GitInfo, Task
from featx.sched import daemon
from featx.tree import Tree, TreeError


def _runset(node: str, *, W: int, I: int, err: float, points=("a_1_0.5", "b_2_0.5"), common=None,
            reference="base", ipc=1.0):
    t = Tree()
    if node not in t.by_rel:
        t.add(node, parents=True)
        t = Tree()
    entry = t.get(node)
    rs, rdir = t.new_runset(entry, purpose=f"err {err}")
    rs.recipes = {"run": {"W": W, "I": I}}
    t.save_runset(entry, rs)
    (rdir / "run").mkdir()
    store.write_json(rdir / "run" / "points.json", list(points))
    step = rdir / "post" / "pk"
    step.mkdir(parents=True)
    store.write_json(step / "summary.json", {
        "reference": reference, "block": 2, "points": len(points),
        "common_points": len(points) if common is None else common,
        "self.mean_abs_err_pct": err, "self.ipc_geomean": ipc})
    return f"{node}/{rs.name}"


# ------------------------------------------------------------ leaderboard
def test_leaderboard_classes_ranking_and_incomplete(root):
    good = _runset("wpe/a", W=20_000_000, I=20_000_000, err=1.5, ipc=1.2)
    best = _runset("wpe/b", W=20_000_000, I=20_000_000, err=0.8, ipc=1.1)
    worse = _runset("wpe/b", W=20_000_000, I=20_000_000, err=2.0)
    partial = _runset("wpe/c", W=20_000_000, I=20_000_000, err=0.1, common=1)
    smoke = _runset("wpe/a", W=1_000_000, I=2_000_000, err=47.3)
    other_pts = _runset("wpe/a", W=20_000_000, I=20_000_000, err=0.2, points=("a_1_0.5",))

    r = leaderboard.build()
    assert r["metric"] == "pk.self.mean_abs_err_pct" and r["order"] == "asc"
    assert len(r["classes"]) == 3                      # window and point set split the classes
    assert "W=20M I=20M" in r["classes"][0]["label"] and r["class"] == r["classes"][0]["key"]
    assert [(x["runset"], x["rank"]) for x in r["rows"]] == [(best, 1), (good, 2), (worse, 3)]
    # a runset that did not finish every point is listed on request but never ranked
    r = leaderboard.build(include_incomplete=True)
    assert [(x["runset"], x["rank"]) for x in r["rows"]] == [(partial, None), (best, 1), (good, 2), (worse, 3)]
    r = leaderboard.build(best_per_node=True)
    assert [x["runset"] for x in r["rows"]] == [best, good]
    r = leaderboard.build(node="wpe/b")
    assert [x["runset"] for x in r["rows"]] == [best, worse]

    smoke_cls = next(c["key"] for c in leaderboard.build()["classes"] if "W=1M I=2M" in c["label"])
    assert [x["runset"] for x in leaderboard.build(cls=smoke_cls)["rows"]] == [smoke]
    assert other_pts not in [x["runset"] for x in leaderboard.build()["rows"]]

    r = leaderboard.build(metric="pk.self.ipc_geomean")
    assert r["order"] == "desc" and r["rows"][0]["runset"] == good
    assert {m["name"] for m in r["metrics"]} >= {"pk.self.mean_abs_err_pct", "pk.self.ipc_geomean", "pk.points"}
    assert "pk.block" not in {m["name"] for m in r["metrics"]}


def test_leaderboard_cache_follows_summary_changes(root):
    ref = _runset("x", W=1, I=1, err=5.0)
    assert leaderboard.build()["rows"][0]["value"] == 5.0
    path = config.tree_root() / ref / "post" / "pk" / "summary.json"
    data = json.loads(path.read_text())
    data["self.mean_abs_err_pct"] = 3.0
    store.write_json(path, data)
    os.utime(path, ns=(1, path.stat().st_mtime_ns + 1_000_000))
    assert leaderboard.build()["rows"][0]["value"] == 3.0


def test_leaderboard_empty(root):
    r = leaderboard.build()
    assert r["metric"] is None and r["rows"] == [] and r["classes"] == []
    assert CliRunner().invoke(cli.app, ["leaderboard"]).output.strip() == "no post summaries yet"


# ----------------------------------------------------------------- config
def test_config_validation_and_roundtrip(root):
    with pytest.raises(ValueError, match="invalid YAML"):
        config.write_user_layer("hosts: [a\n")
    with pytest.raises(ValueError, match="nosuch: unknown key"):
        config.write_user_layer("nosuch: 1\n")
    with pytest.raises(ValueError, match="retry.max_infra_attempts: expected a number"):
        config.write_user_layer("retry: {max_infra_attempts: many}\n")
    with pytest.raises(ValueError, match="hosts: expected list"):
        config.write_user_layer("hosts: node1\n")
    before = (root / "featx.yaml").read_text()          # a rejected write leaves the file alone
    assert yaml.safe_load(before)["hosts"] == ["localhost"]

    r = config.write_user_layer("hosts: [localhost]\nretry: {max_infra_attempts: 7}\ndaemon: {port: 9999}\n")
    assert r["changed"] == ["daemon", "liveness", "retry"] and r["restart_needed"] == ["daemon"]
    assert config.settings()["retry"]["max_infra_attempts"] == 7
    v = config.config_view()
    assert v["user"]["daemon"] == {"port": 9999} and v["effective"]["daemon"]["host"] is None


def test_config_cli_set(root):
    run = CliRunner().invoke
    r = run(cli.app, ["config", "set", "retry.max_infra_attempts=5", "placement.reserve_cores=2"])
    assert r.exit_code == 0, r.output
    s = config.settings()
    assert s["retry"]["max_infra_attempts"] == 5 and s["placement"]["reserve_cores"] == 2
    assert s["hosts"] == ["localhost"]                  # the rest of the user layer survives
    r = run(cli.app, ["config", "set", "bogus=1"])
    assert r.exit_code == 1 and "unknown key" in r.output
    assert "max_infra_attempts: 5" in run(cli.app, ["config", "show"]).output


# ------------------------------------------------------------------ files
def test_files_confined_to_roots(root, tmp_path):
    (root / "tree").mkdir(exist_ok=True)
    (root / "tree" / "note.md").write_text("hello\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("s3cret")
    os.symlink(secret, root / "tree" / "leak.txt")
    assert files.read("tree/note.md")["text"] == "hello\n"
    assert files.read(str(root / "tree" / "note.md"))["size"] == 6
    for bad in ("../secret.txt", str(secret), "/etc/passwd", "tree/leak.txt", "tree/../../secret.txt"):
        with pytest.raises(PermissionError):
            files.read(bad)
    with pytest.raises(FileNotFoundError):
        files.read("tree/absent.md")
    names = [f["path"] for f in files.listing("tree")["files"]]
    assert "note.md" in names


def test_files_tail_and_truncation(root):
    log = root / "big.log"
    log.write_text("".join(f"line {i}\n[PERF ] x\n" for i in range(2000)))
    r = files.read("big.log", tail=3, skip_perf=True)
    assert r["text"].splitlines() == ["line 1997", "line 1998", "line 1999"]
    r = files.read("big.log", max_bytes=1000)
    assert r["truncated"] and r["text"].rstrip().endswith("[PERF ] x")     # logs keep their tail
    csv = root / "big.csv"
    csv.write_text("h1,h2\n" + "1,2\n" * 2000)
    assert files.read("big.csv", max_bytes=1000)["text"].startswith("h1,h2")  # tables keep their head


# ------------------------------------------------------------- mv dry run
def _snapshot(path):
    out = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            out.append((os.path.relpath(p, path), os.path.islink(p)))
    return sorted(out)


def _git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_mv_dry_run_matches_real_move(root, tmp_path):
    t = Tree()
    t.add("cat/feat/sub", parents=True)
    t = Tree()
    t.new_runset(t.get("cat/feat/sub"))
    repo = tmp_path / "repo"
    _git("init", "-q", "-b", "main", str(repo))
    _git("commit", "-q", "--allow-empty", "-m", "base", cwd=repo)
    wt_dir = config.wt_root() / "cat" / "feat"
    wt_dir.parent.mkdir(parents=True)
    _git("worktree", "add", "-q", "-b", "featx/cat/feat", str(wt_dir), "main", cwd=repo)
    t = Tree()
    e = t.get("cat/feat")
    e.node.git = GitInfo(repo=str(repo), base="main", branch="featx/cat/feat", worktree=str(wt_dir))
    t.save(e)

    before = (_snapshot(config.tree_root()), _snapshot(config.wt_root()))
    plan = Tree().mv("cat/feat", "other/renamed", parents=True, dry_run=True)
    assert plan["dry_run"] and plan["create_parents"] == ["other"]
    assert [(n["from"], n["to"], n["kind"], n["runsets"]) for n in plan["nodes"]] == [
        ("cat/feat", "other/renamed", "feature", 0), ("cat/feat/sub", "other/renamed/sub", "subfeature", 1)]
    git_plan = plan["nodes"][0]["git"]
    assert git_plan["worktree"]["to"] == str(config.wt_root() / "other" / "renamed")
    assert git_plan["branch"]["to"] == "other/renamed"
    # nothing moved, nothing renamed
    assert (_snapshot(config.tree_root()), _snapshot(config.wt_root())) == before
    assert "other" not in Tree().by_rel
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
                              capture_output=True, text=True).stdout.split()
    assert "featx/cat/feat" in branches and "other/renamed" not in branches

    real = Tree().mv("cat/feat", "other/renamed", parents=True)
    assert [(n["from"], n["to"]) for n in real["nodes"]] == [(n["from"], n["to"]) for n in plan["nodes"]]
    moved = Tree().get("other/renamed").node.git
    assert moved.worktree == git_plan["worktree"]["to"] and moved.branch == git_plan["branch"]["to"]


def test_mv_dry_run_runs_the_same_checks(root):
    t = Tree()
    t.add("a/b", parents=True)
    t.add("c", parents=True)
    with pytest.raises(TreeError, match="exists"):
        Tree().mv("a/b", "c", dry_run=True)
    with pytest.raises(TreeError):
        Tree().mv("a/b", "nope/b", dry_run=True)       # parent missing without parents=True
    with pytest.raises(TreeError):
        Tree().mv("a", "a/b/x", dry_run=True)
    node_id = Tree().get("a/b").node.id
    taskstore.save(Task(id=store.new_id(), kind="run", name="run", node_id=node_id, runset_id="r",
                        runset_name="20261012-1", state="running"))
    with pytest.raises(TreeError, match="active tasks"):
        Tree().mv("a", "d", dry_run=True)


# ------------------------------------------------------------ REST + UI
def test_rest_ui_endpoints(root, monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>featx-ui</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "outside.txt").write_text("no")
    monkeypatch.setattr(daemon, "UI_DIST", dist)
    d = daemon.Daemon(port=1)
    c = TestClient(daemon.build_app(d), follow_redirects=False)
    h = {"X-Featx-Token": d.token}

    # the bundle is public, the API is not
    assert c.get("/").headers["location"] == "/ui/"
    assert "featx-ui" in c.get("/ui/").text
    assert "featx-ui" in c.get("/ui/runset/wpe/a/20261012-1").text       # SPA fallback
    assert c.get("/ui/assets/app.js").text == "console.log(1)"
    assert c.get("/ui/../outside.txt").status_code == 401                 # normalized to /outside.txt
    assert "featx-ui" in c.get("/ui/%2e%2e/outside.txt").text            # never leaves the bundle dir
    for path in ("/leaderboard", "/config", "/files?path=featx.yaml", "/files/list?path=tree"):
        assert c.get(path).status_code == 401, path

    ref = _runset("wpe/a", W=1, I=1, err=2.5)
    r = c.get("/leaderboard", headers=h).json()
    assert r["rows"][0]["runset"] == ref and r["rows"][0]["rank"] == 1

    assert c.get("/config", headers=h).json()["user"]["hosts"] == ["localhost"]
    r = c.put("/config", json={"text": "bogus: 1\n"}, headers=h)
    assert r.status_code == 400 and "unknown key" in r.json()["error"]
    r = c.put("/config", json={"text": "hosts: [localhost]\nplacement: {reserve_cores: 1}\n"}, headers=h)
    assert r.status_code == 200 and r.json()["changed"] == ["liveness", "placement", "retry"]

    r = c.get("/files", params={"path": f"tree/{ref}/post/pk/summary.json"}, headers=h)
    assert r.status_code == 200 and json.loads(r.json()["text"])["self.mean_abs_err_pct"] == 2.5
    assert c.get("/files", params={"path": "/etc/passwd"}, headers=h).status_code == 403
    assert c.get("/files", params={"path": "../outside.txt"}, headers=h).status_code == 403
    listed = c.get("/files/list", params={"path": f"tree/{ref}", "depth": 3}, headers=h).json()
    assert "post/pk/summary.json" in [f["path"] for f in listed["files"]]

    r = c.post("/nodes/mv", json={"src": "wpe/a", "dst": "wpe/z", "dry_run": True}, headers=h)
    assert r.status_code == 200 and r.json()["dry_run"] and "wpe/a" in Tree().by_rel


def test_ui_not_built_message(root, monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "UI_DIST", tmp_path / "nodist")
    c = TestClient(daemon.build_app(daemon.Daemon(port=1)))
    r = c.get("/ui/")
    assert r.status_code == 503 and "npm run build" in r.text


def test_cli_preview_and_mv_dry_run(root):
    run = CliRunner().invoke
    Tree().add("a/b", parents=True)
    (root / "tree" / "a" / "b" / "notes.md").write_text("one\ntwo\nthree\n")
    r = run(cli.app, ["preview", "tree/a/b/notes.md", "--tail", "2"])
    assert r.exit_code == 0 and r.output.split() == ["two", "three"]
    assert "notes.md" in run(cli.app, ["preview", "tree/a/b"]).output
    r = run(cli.app, ["preview", "/etc/passwd"])
    assert r.exit_code == 1 and "outside" in r.output
    r = run(cli.app, ["node", "mv", "a/b", "a/c", "--dry-run"])
    assert r.exit_code == 0 and json.loads(r.output)["nodes"][0]["to"] == "a/c"
    assert "a/b" in Tree().by_rel
