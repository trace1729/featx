import json
import os
import time

import pytest

from featx import config, problemlog, provenance, recipes, service, store, taskstore
from featx.model import Task
from featx.sched import classify, eta
from featx.tree import Tree, TreeError


# ------------------------------------------------------------------ tree
def test_add_kinds_and_parents(root):
    with pytest.raises(TreeError):
        Tree().add("wpe/real/after")
    e = Tree().add("wpe/real/after", parents=True, hypothesis="h")
    t = Tree()
    assert [t.get(r).node.kind for r in ("wpe", "wpe/real", "wpe/real/after")] == \
        ["category", "feature", "subfeature"]
    assert t.get(e.node.id).rel == "wpe/real/after"
    assert t.get(e.node.id[:12]).rel == "wpe/real/after"


@pytest.mark.parametrize("bad", ["a/run", "a/20261012-1", "a/.x", "a/b c"])
def test_reserved_names(root, bad):
    with pytest.raises(TreeError):
        Tree().add(bad, parents=True)


def test_mv_cascade_symlink_and_active_refusal(root):
    t = Tree()
    t.add("cat/feat/sub", parents=True)
    t = Tree()
    rs, rdir = t.new_runset(t.get("cat/feat/sub"), purpose="p")
    (rdir / "run").mkdir()
    (rdir / "run" / "marker").write_text("1")
    sub_id = Tree().get("cat/feat/sub").node.id

    task = Task(id=store.new_id(), kind="run", name="run", node_id=sub_id, runset_id=rs.id,
                runset_name=rs.name, state="running")
    taskstore.save(task)
    with pytest.raises(TreeError, match="active tasks"):
        Tree().mv("cat/feat", "cat2/feat", parents=True)
    task.state = "succeeded"
    taskstore.save(task)

    rep = Tree().mv("cat/feat", "cat2/feat", parents=True)
    assert {n["to"] for n in rep["nodes"]} == {"cat2/feat", "cat2/feat/sub"}
    t = Tree()
    assert t.get(sub_id).rel == "cat2/feat/sub"
    assert t.get(sub_id).node.moves[-1]["from"] == "cat/feat/sub"
    # old absolute paths keep resolving through the forwarding symlink
    old = config.tree_root() / "cat/feat/sub" / rs.name / "run" / "marker"
    assert old.read_text() == "1"
    assert (config.tree_root() / "cat/feat").is_symlink()
    # the runset ref under the new path resolves
    entry, rs2 = t.find_runset(f"cat2/feat/sub/{rs.name}")
    assert rs2.id == rs.id
    # a forwarding symlink does not show up as a node
    assert "cat/feat" not in t.by_rel


def test_update_and_rm(root):
    Tree().add("a/b", parents=True)
    service.node_update("a/b", verdict="refuted", add_evidence=["a/b/20261012-1"], add_tags=["x"])
    n = Tree().get("a/b").node
    assert (n.verdict, n.evidence, n.tags) == ("refuted", ["a/b/20261012-1"], ["x"])
    with pytest.raises(TreeError):
        service.node_update("a/b", verdict="maybe")
    rep = service.node_rm("a")
    assert "a" not in Tree().by_rel and os.path.isdir(rep["trash"])
    assert service.gc(apply=False)["candidates"][0]["kind"] == "trash"


def test_runset_numbering(root):
    Tree().add("x", parents=True)
    t = Tree()
    a, _ = t.new_runset(t.get("x"))
    b, _ = t.new_runset(t.get("x"))
    assert a.name.endswith("-1") and b.name.endswith("-2") and a.name[:8] == store.today()


# --------------------------------------------------------------- recipes
def test_recipe_inheritance(root):
    Tree().add("c/f/s", parents=True)
    store.write_yaml(root / "recipes" / "run.yaml", {"W": 1, "env": {"A": "root"}})
    service.recipe_set("c", "run", {"I": 5, "env": {"B": "cat"}})
    service.recipe_set("c/f/s", "run", {"W": 3, "env": {"A": None}, "args": "--x ${W}"}, merge=True)
    t = Tree()
    eff = recipes.effective(t, t.get("c/f/s"), "run")
    assert eff["W"] == 3 and eff["I"] == 5
    assert eff["env"] == {"B": "cat"}                       # None deletes an inherited key
    assert eff["executor"] == "points"                       # package default survives
    sub = recipes.substitute(eff, recipes.scalar_vars(eff))
    assert sub["args"] == "--x 3"
    assert "c/f/s" in recipes.explain(t, t.get("c/f/s"), "run")


# -------------------------------------------------------------- classify
@pytest.mark.parametrize("kind,rc,text,cls,sig", [
    ("run", 1, "xx\nTraceRTL Error at inst 12345\n", "design", "TraceRTL Error at inst 12345"),
    ("run", 1, "TraceRTL Stuck at inst 9 \n", "design", "TraceRTL Stuck at inst 9"),
    ("run", 255, "Connection closed by UNKNOWN port 65535", "infra", None),
    ("run", 137, "Killed", "infra", "killed by signal (rc=137)"),
    ("run", 137, "ABORT at pc = 0x80000000", "design", "ABORT at pc = 0x80000000"),
    ("build", 2, "foo.cpp:12:3: error: bad thing\nmake: *** [x] Error 1", "design", "foo.cpp:12:3: error: bad thing"),
    ("run", None, "featx: heartbeat stale on node1 (alive=False)", "infra", None),
    ("run", 7, "nothing useful", "design", "exit code 7"),
    ("post", 1, "Traceback...\nKeyError: 'ipc'", "design", "KeyError: 'ipc'"),
])
def test_classify(root, kind, rc, text, cls, sig):
    v = classify.classify(kind, rc, text)
    assert v["class"] == cls
    if sig:
        assert v["signature"] == sig


def test_tail_skips_perf(tmp_path):
    p = tmp_path / "err"
    p.write_text("\x1b[31mred\x1b[0m\n" + "[PERF ][time=1] a: b, 1\n" * 1000 + "last\n")
    t = classify.tail_text(p)
    assert "PERF" not in t and "red" in t and t.strip().endswith("last")


# ------------------------------------------------------------------- eta
def test_progress_parse_and_remaining(tmp_path):
    p = tmp_path / "out"
    p.write_bytes(b"Current Finished Inst Num 100000. Simulation Speed: 10 cycle/s 1000 inst/s"
                  b"Current Finished Inst Num 200000. Simulation Speed: 10 cycle/s 2000 inst/s")
    prog = eta.parse_progress(p)
    assert prog["inst"] == 200000 and prog["inst_per_s"] == 1500
    assert eta.job_remaining(prog, 500000, time.time(), None) == pytest.approx(200.0)
    assert eta.fmt_duration(30) == "30s" and eta.fmt_duration(7200) == "2h00m"


def test_history_median(root):
    h = eta.History()
    for d in (10, 20, 30):
        h.record("run:a_1_0.5:1:2", d, True)
    assert h.median("run:a_1_0.5:1:2") == 20
    assert h.median("run:a_1_0.5:9:9") == 20    # falls back to the point regardless of W/I
    assert h.generic_run() == 20


# ------------------------------------------------------------ provenance
def test_bincache_key_ignores_placement_fields(root):
    state = {"super": {"head": "abc", "diff_sha": "1"}}
    a = provenance.bincache_key(state, {"type": "x", "cores": 64, "host": "n1", "gsim": {"jobs": 1}})
    b = provenance.bincache_key(state, {"type": "x", "cores": 8, "host": None, "gsim": {"jobs": 1}})
    c = provenance.bincache_key(state, {"type": "x", "gsim": {"jobs": 2}})
    assert a == b != c


def test_bin_import_dedup(root, tmp_path):
    f = tmp_path / "emu"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    k1 = service.bin_import(str(f), "l")["key"]
    k2 = service.bin_import(str(f), "l")["key"]
    assert k1 == k2 and provenance.bincache_lookup(k1)


# ----------------------------------------------------------- problem log
def test_problem_log_format_and_guard(root):
    Tree().add("n", parents=True)
    t = Tree()
    rs, rdir = t.new_runset(t.get("n"))
    before = time.time()
    assert not problemlog.analysed_since(rdir, before)
    service.problem_log_append(f"n/{rs.name}", problem="P", solution="S", code="C")
    text = (rdir / problemlog.LOG_NAME).read_text()
    assert text.startswith("## 20") and text.index("问题与解决方案总结") < text.index("代码修改说明")
    assert problemlog.analysed_since(rdir, before)
    with pytest.raises(ValueError):
        service.problem_log_append(f"n/{rs.name}", problem="", solution="S")


def _git(*args, cwd=None):
    import subprocess
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_rm_drop_worktrees_cleans_branches_and_dirs(root, tmp_path):
    from featx.model import GitInfo, SubWorktree
    wt_root = tmp_path / "wt"
    sup, sub = tmp_path / "super", tmp_path / "sub"
    for r in (sup, sub):
        _git("init", "-q", "-b", "main", str(r))
        _git("commit", "-q", "--allow-empty", "-m", "base", cwd=r)
    new, old = wt_root / "a/new/x", wt_root / "a/old/x"
    _git("worktree", "add", "-q", "-b", "featx/a/new/x", str(new), "main", cwd=sup)
    _git("worktree", "add", "-q", "-b", "featx/a/new/x", str(new / "sub"), "main", cwd=sub)
    _git("commit", "-q", "--allow-empty", "-m", "own work", cwd=new / "sub")   # unmerged commit
    old.parent.mkdir(parents=True)
    os.symlink(new, old)                                                       # forwarding link of a mv
    e = Tree().add("a/new/x", parents=True)
    e.node.git = GitInfo(repo=str(sup), base="main", branch="featx/a/new/x", worktree=str(new),
                         submodule_worktrees={"sub": SubWorktree(repo=str(sub), base="main",
                                                                 branch="featx/a/new/x",
                                                                 path=str(new / "sub"))})
    e.node.moves = [{"from": "a/old/x", "to": "a/new/x",
                     "git": {"worktree": {"from": str(old), "to": str(new)}}}]
    store.write_yaml(e.dir / "node.yaml", e.node.model_dump())

    rep = service.node_rm("a", force=True, drop_worktrees=True)
    br = rep["worktrees"][0]["branches"]
    assert br[str(sup)] == "deleted" and br[str(sub)].startswith("kept (1 commits")
    assert not old.is_symlink() and not (wt_root / "a").exists() and wt_root.is_dir()

    _git("worktree", "add", "-q", str(tmp_path / "w2"), "featx/a/new/x", cwd=sub)  # ensure branch intact
    _git("worktree", "remove", str(tmp_path / "w2"), cwd=sub)
    e = Tree().add("b", parents=True)
    e.node.git = GitInfo(repo=str(sup), base="main", branch="featx/b", worktree=str(wt_root / "b"),
                         submodule_worktrees={"sub": SubWorktree(repo=str(sub), base="main",
                                                                 branch="featx/a/new/x",
                                                                 path=str(wt_root / "b/sub"))})
    store.write_yaml(e.dir / "node.yaml", e.node.model_dump())
    rep = service.node_rm("b", force=True, drop_worktrees=True, drop_branches=True)
    assert rep["worktrees"][0]["branches"] == {str(sup): "absent", str(sub): "deleted"}


def test_bincache_key_scripts_by_content(root, tmp_path):
    state = {"super": {"head": "abc"}}
    plain = {"type": "script", "script": "make"}
    assert provenance.bincache_key(state, plain, tmp_path / "rs1" / "scripts") == provenance.bincache_key(state, plain)
    for rs in ("rs1", "rs2"):
        (tmp_path / rs / "scripts").mkdir(parents=True)
        (tmp_path / rs / "scripts" / "b.sh").write_text("make\n")
    k1 = provenance.bincache_key(state, {"type": "script", "script": f"bash {tmp_path}/rs1/scripts/b.sh"},
                                 tmp_path / "rs1" / "scripts")
    k2 = provenance.bincache_key(state, {"type": "script", "script": f"bash {tmp_path}/rs2/scripts/b.sh"},
                                 tmp_path / "rs2" / "scripts")
    assert k1 == k2
    (tmp_path / "rs2" / "scripts" / "b.sh").write_text("make -j\n")
    assert provenance.bincache_key(state, {"type": "script", "script": f"bash {tmp_path}/rs2/scripts/b.sh"},
                                   tmp_path / "rs2" / "scripts") != k1


def test_snapshot_scripts_layers_and_prunes(root, tmp_path):
    from featx import recipes, service
    tree = Tree()
    tree.add("a/b", parents=True)
    (root / "scripts").mkdir()
    (root / "scripts" / "common.sh").write_text("root")
    service.script_write("a", "x.sh", "parent")
    service.script_write("a/b", "x.sh", "child")
    tree = Tree()
    entry = tree.get("a/b")
    dest = recipes.snapshot_scripts(tree, entry, tmp_path / "rs")
    assert {p.name: p.read_text() for p in dest.iterdir()} == {"common.sh": "root", "x.sh": "child"}
    assert recipes.base_context(tree, entry, tmp_path / "rs")["scripts"] == str(dest)
    (entry.dir / "scripts" / "x.sh").unlink()
    recipes.snapshot_scripts(tree, entry, tmp_path / "rs")
    assert (dest / "x.sh").read_text() == "parent"
    (root / "scripts" / "common.sh").unlink()
    recipes.snapshot_scripts(tree, entry, tmp_path / "rs")
    assert sorted(p.name for p in dest.iterdir()) == ["x.sh"]
