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
    service.node_update("x", comment="remember this", add_evidence=[f"x/{b.name}"])
    brief = service.node_brief("x", 0)
    assert brief["runsets"] == [] and brief["runset_count"] == 2
    assert brief["node"]["comment"] == "remember this" and brief["node"]["evidence"] == [f"x/{b.name}"]
    assert "recipes" not in brief
    with pytest.raises(ValueError, match="non-negative"):
        service.node_brief("x", -1)


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
    ("post", 3, "check_paired: nothing to compare: the reference has no valid point\n  trace a: 1/1",
     "design", "check_paired: nothing to compare: the reference has no valid point"),
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


def test_rm_keeps_adopted_worktree(root, tmp_path):
    """`node rm --drop-worktrees` must not delete a checkout the node only adopted."""
    from featx.model import GitInfo
    repo = tmp_path / "repo"
    _git("init", "-q", "-b", "main", str(repo))
    _git("commit", "-q", "--allow-empty", "-m", "base", cwd=repo)
    wt = tmp_path / "wt" / "user" / "checkout"
    _git("worktree", "add", "-q", "-b", "user/topic", str(wt), "main", cwd=repo)
    (wt / "dirty.txt").write_text("work in progress\n")

    e = Tree().add("adopted", parents=True)
    e.node.git = GitInfo(repo=str(repo), base="main", branch="user/topic", worktree=str(wt), managed=False)
    store.write_yaml(e.dir / "node.yaml", e.node.model_dump())

    rep = service.node_rm("adopted", force=True, drop_worktrees=True, drop_branches=True)
    assert rep["worktrees"][0]["kept"].startswith("adopted")
    assert not (config.tree_root() / "adopted").exists()          # the node record is gone
    assert wt.is_dir() and (wt / "dirty.txt").read_text() == "work in progress\n"
    assert "user/topic" in _git_branches(repo)                    # branch untouched too


def test_rm_keeps_worktree_shared_with_another_node(root, tmp_path):
    """Two nodes can record one checkout; removing one must not delete it under the other."""
    from featx.model import GitInfo
    repo = tmp_path / "repo"
    _git("init", "-q", "-b", "main", str(repo))
    _git("commit", "-q", "--allow-empty", "-m", "base", cwd=repo)
    wt = tmp_path / "wt" / "shared"
    _git("worktree", "add", "-q", "-b", "featx/owner", str(wt), "main", cwd=repo)

    for rel in ("owner", "dup"):
        e = Tree().add(rel)
        path = str(wt) if rel == "owner" else str(wt) + "/"    # trailing slash must not defeat the guard
        e.node.git = GitInfo(repo=str(repo), base="main", branch=f"featx/{rel}", worktree=path,
                             managed=True)
        store.write_yaml(e.dir / "node.yaml", e.node.model_dump())

    rep = service.node_rm("dup", force=True, drop_worktrees=True)
    assert "owner" in rep["worktrees"][0]["kept"]
    assert not (config.tree_root() / "dup").exists()
    assert wt.is_dir() and (config.tree_root() / "owner").exists()
    assert "featx/owner" in _git_branches(repo)


def _git_branches(repo):
    import subprocess
    out = subprocess.run(["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
                         check=True, stdout=subprocess.PIPE, text=True).stdout
    return out.split()


def test_branch_naming_ownership_and_conflicts(root, tmp_path):
    from featx import worktree
    from featx.model import GitInfo
    assert worktree.default_branch("wpe/overlay/smoke") == "wpe/overlay/smoke"
    repo = tmp_path / "r"
    _git("init", "-q", "-b", "main", str(repo))
    _git("commit", "-q", "--allow-empty", "-m", "base", cwd=repo)
    _git("branch", "wpe", cwd=repo)
    _git("branch", "x/y/z", cwd=repo)
    assert "already exists" in worktree.branch_conflict(str(repo), "main")
    assert "branch wpe exists" in worktree.branch_conflict(str(repo), "wpe/overlay/smoke")
    assert "branch x/y/z exists" in worktree.branch_conflict(str(repo), "x/y")
    assert worktree.branch_conflict(str(repo), "x/q") is None
    assert worktree.branch_conflict(str(repo), "wpe", ignore="wpe") is None
    g = lambda **kw: GitInfo(repo=str(repo), base="main", worktree="/w", **kw)
    assert worktree.managed(g(branch="featx/a"))                  # pre-`managed` record, old prefix
    assert not worktree.managed(g(branch="trace-wpe-0910"))       # pre-`managed` adopted user branch
    assert worktree.managed(g(branch="a/b", managed=True)) and not worktree.managed(g(branch="a/b", managed=False))
    # a move whose new branch name clashes keeps the old branch instead of failing
    e = Tree().add("wpe2", parents=True)
    e.node.git = g(branch="featx/old", managed=None)
    _git("branch", "featx/old", cwd=repo)
    assert "branch_kept" in worktree.plan_relocate(e.node, "wpe/overlay")
    assert worktree.plan_relocate(e.node, "fresh/name")["branch"]["to"] == "fresh/name"
    # an old featx/<path> branch can be renamed to the current default (the node path)
    store.write_yaml(e.dir / "node.yaml", e.node.model_dump())
    assert service.worktree_rename_branch("wpe2", dry_run=True)["to"] == "wpe2"
    assert service.worktree_rename_branch("wpe2") == {"from": "featx/old", "to": "wpe2", "submodules": []}
    assert Tree().get("wpe2").node.git.branch == "wpe2" and Tree().get("wpe2").node.git.managed
    assert worktree.branch_conflict(str(repo), "wpe2") is not None
    with pytest.raises(TreeError, match="already exists"):
        service.worktree_rename_branch("wpe2", to="main")


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


def test_update_allowlist_evidence_edit_and_comment(root):
    Tree().add("a/b", parents=True)
    service.node_update("a/b", add_evidence=["a/b/20261012-1", "a/b/20261012-2", "note x"])
    service.node_update("a/b", rm_evidence=["note x"], comment="try a wider window\nnext")
    n = Tree().get("a/b").node
    assert n.evidence == ["a/b/20261012-1", "a/b/20261012-2"] and n.comment == "try a wider window\nnext"
    # the full list replaces (edit + reorder), blanks and duplicates dropped
    service.node_update("a/b", evidence=["a/b/20261012-2", " ", "a/b/20261012-3", "a/b/20261012-2"])
    assert Tree().get("a/b").node.evidence == ["a/b/20261012-2", "a/b/20261012-3"]
    assert service.node_context("a/b")["node"]["comment"].startswith("try")
    for bad in ({"git": {}}, {"moves": []}, {"created": "x"}, {"id": "y"}, {"evidence": "a/b/1"},
                {"comment": 3}):
        with pytest.raises(TreeError):
            service.node_update("a/b", **bad)


def test_set_user_key(root):
    res = config.set_user_key("placement.max_total_cores", 40)
    assert res["changed"] == ["placement"] and config.settings()["placement"]["max_total_cores"] == 40
    assert config.settings()["hosts"] == ["localhost"]          # the rest of the layer survives
    config.set_user_key("placement.max_total_cores", None)
    assert config.settings()["placement"]["max_total_cores"] == config.defaults()["placement"]["max_total_cores"]
    for key, value in (("placement.nope", 1), ("placement.max_total_cores", "many"), ("zzz", 1)):
        with pytest.raises(ValueError):
            config.set_user_key(key, value)


def test_review_state_tracks_completion_and_research_record(root):
    from featx import context

    entry = Tree().add("review")
    rs, _ = Tree().new_runset(entry)
    rs.tasks = ["TASK1"]
    Tree().save_runset(entry, rs)
    task = Task(id="TASK1", kind="run", name="run", node_id=entry.node.id,
                runset_id=rs.id, runset_name=rs.name, state="running")
    taskstore.save(task)
    ref = f"review/{rs.name}"
    assert service.result_get(ref)["review"]["complete"] is False

    for state, outcome in (("succeeded", "succeeded"), ("failed", "failed"), ("cancelled", "cancelled")):
        task.state = state
        taskstore.save(task)
        review = service.result_get(ref)["review"]
        assert review["needed"] and review["complete"] and review["outcome"] == outcome
        assert review["command"] == f"featx node set review --verdict VERDICT --evidence {ref}"

    entry.node.verdict = "supported"
    entry.node.evidence = []
    assert context.review_state(entry, rs, {"TASK1": task})["needed"]
    entry.node.evidence = [ref]
    assert not context.review_state(entry, rs, {"TASK1": task})["needed"]
    entry.node.verdict = "open"
    assert context.review_state(entry, rs, {"TASK1": task})["needed"]


def test_worktree_changes_and_diff(root, tmp_path):
    from featx import worktree
    from featx.model import GitInfo
    repo, wt = tmp_path / "repo", tmp_path / "wt" / "c"
    _git("init", "-q", "-b", "main", str(repo))
    (repo / "a.txt").write_text("one\n")
    (repo / ".gitmodules").write_text('[submodule "shared"]\n\tpath = shared\n\turl = x\n')
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    base = worktree.validate_base(str(repo), "main")
    _git("worktree", "add", "-q", "-b", "c", str(wt), "main", cwd=repo)
    (wt / "a.txt").write_text("one\ntwo\n")
    _git("commit", "-q", "-am", "add two", cwd=wt)
    (wt / "b.txt").write_text("new\n")
    _git("add", "b.txt", cwd=wt)
    _git("commit", "-q", "-m", "add b", cwd=wt)
    (wt / "b.txt").write_text("new\nmore\n")              # uncommitted
    (wt / "u.txt").write_text("untracked\n")
    os.symlink(tmp_path, wt / "shared")                   # a shared submodule symlink: not a change
    _git("commit", "-q", "--allow-empty", "-m", "upstream", cwd=repo)   # the original ref moves on
    e = Tree().add("c")
    e.node.git = GitInfo(repo=str(repo), base=base, branch="c", worktree=str(wt))
    store.write_yaml(e.dir / "node.yaml", e.node.model_dump())

    ch = service.worktree_changes("c")
    r = ch["repos"][""]
    assert r["ahead"] == 2 and r["base"] == base
    assert "merge_base" not in r and "behind" not in r
    assert [c["subject"] for c in r["commits"]] == ["add b", "add two"]
    assert all(len(c["sha"]) == 40 for c in r["commits"])
    files = {f["path"]: f for f in r["files"]}
    assert set(files) == {"a.txt", "b.txt"}
    assert files["b.txt"]["status"] == "A" and files["b.txt"]["added"] == 2 and files["b.txt"]["uncommitted"]
    assert files["a.txt"]["added"] == 1 and not files["a.txt"]["uncommitted"]
    assert r["untracked"] == ["u.txt"]
    assert ch["total"] == {"files": 3, "added": 3, "deleted": 0, "ahead": 2}
    st = service.worktree_status("c")
    assert st["ahead"] == 2 and st["files_changed"] == 2 and st["base"] == base
    assert len(st["head"]) == 40 and "merge_base" not in st and "behind" not in st
    d = service.worktree_diff("c", "", "a.txt")["diff"]
    assert "+two" in d and "a/a.txt" in d
    assert "+untracked" in service.worktree_diff("c", "", "u.txt")["diff"]
    with pytest.raises(TreeError):
        service.worktree_diff("c", "", "../etc/passwd")
    with pytest.raises(TreeError):
        service.worktree_diff("c", "TraceRTL", "a.txt")


def test_worktree_base_validation_and_repair(root, tmp_path):
    import json
    from typer.testing import CliRunner
    from featx import cli, worktree

    repo, wt = tmp_path / "repo", tmp_path / "wt"
    sub, sub_wt = tmp_path / "sub", wt / "TraceRTL"
    for path in (repo, sub):
        _git("init", "-q", "-b", "main", str(path))
        _git("commit", "-q", "--allow-empty", "-m", "base", cwd=path)
    root_base = worktree.validate_base(str(repo), "main")
    sub_base = worktree.validate_base(str(sub), "main")
    _git("worktree", "add", "-q", "-b", "feature", str(wt), "main", cwd=repo)
    _git("worktree", "add", "-q", "--detach", str(sub_wt), "main", cwd=sub)
    Tree().add("adopt")
    with pytest.raises(TreeError, match="does not resolve"):
        service.worktree_create("adopt", repo=str(repo), path=str(wt), base="missing", adopt=True,
                                submodules=[])
    assert Tree().get("adopt").node.git is None

    with pytest.raises(TreeError, match="TraceRTL base"):
        service.worktree_create("adopt", repo=str(repo), path=str(wt), base="main", adopt=True,
                                submodules=["TraceRTL"], sub_base={"TraceRTL": "missing"})
    assert Tree().get("adopt").node.git is None
    service.worktree_create("adopt", repo=str(repo), path=str(wt), base="main", adopt=True,
                            submodules=["TraceRTL"], sub_base={"TraceRTL": "main"})
    recorded = Tree().get("adopt").node.git
    assert recorded.base == root_base
    assert recorded.submodule_worktrees["TraceRTL"].base == sub_base
    status = service.worktree_status("adopt")
    assert status["base"] == root_base and status["submodules"]["TraceRTL"]["base"] == sub_base
    assert len(status["head"]) == 40 and len(status["submodules"]["TraceRTL"]["head"]) == 40
    assert "merge_base" not in status and "merge_base" not in status["submodules"]["TraceRTL"]
    entry = Tree().get("adopt")
    entry.node.git.submodule_worktrees["TraceRTL"].base = "main"       # legacy stored ref
    Tree().save(entry)
    assert service.worktree_status("adopt")["submodules"]["TraceRTL"]["base"] == sub_base
    assert service.worktree_set_base("adopt", "main", "TraceRTL")["submodules"]["TraceRTL"]["ahead"] == 0
    with pytest.raises(TreeError, match="does not resolve"):
        service.worktree_set_base("adopt", "missing", "TraceRTL")
    assert Tree().get("adopt").node.git.submodule_worktrees["TraceRTL"].base == sub_base

    result = CliRunner().invoke(cli.app, ["worktree", "changes", "adopt", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total"]["ahead"] == 0 and payload["repos"][""]["base"] == root_base
    assert "merge_base" not in payload["repos"][""] and "behind" not in payload["repos"][""]
    text = CliRunner().invoke(cli.app, ["worktree", "changes", "adopt"])
    assert "Submodule gitlinks are excluded" in text.output
    assert root_base[:12] in text.output and "merge-base" not in text.output and "behind" not in text.output

    Tree().add("create")
    dest = tmp_path / "new-worktree"
    with pytest.raises(TreeError, match="does not resolve"):
        service.worktree_create("create", repo=str(repo), path=str(dest), base="missing", submodules=[])
    assert not dest.exists() and Tree().get("create").node.git is None
    created = service.worktree_create("create", repo=str(repo), path=str(dest), base="main", submodules=[])
    assert created["base"] == root_base and Tree().get("create").node.git.base == root_base
    _git("commit", "-q", "--allow-empty", "-m", "upstream", cwd=repo)
    assert service.worktree_status("create")["base"] == root_base
    assert service.worktree_changes("create")["repos"][""]["base"] == root_base


def test_result_summary_digests_submit_time_echo_and_lists_failures(root):
    from featx.model import Job, Task
    e = Tree().add("r/x", parents=True)
    rs, rdir = Tree().new_runset(e, purpose="p")
    rs.recipes = {"run": {"W": 1, "benchmarks": [f"b{i}" for i in range(50)]}}
    rs.provenance = {"super": {"head": "abc", "dirty": True},
                     "submodules": {"a": {"path": "/a", "head": "1", "dirty": True},
                                    "b": {"path": "/b", "head": "2", "dirty": False}},
                     "saved_patches": ["p"]}
    rs.overrides = {"run": {"W": 1}}
    t = Task(id="T1", kind="run", name="run", node_id=e.node.id, runset_id=rs.id,
             runset_name=rs.name, state="failed",
             jobs=[Job(name="ok", rel="run/ok", state="succeeded"),
                   Job(name="bad", rel="run/bad", state="failed", rc=1,
                       failure={"class": "design", "signature": "assert-mismatch"})])
    taskstore.save(t)
    rs.tasks = ["T1"]
    Tree().save_runset(e, rs)
    (rdir / "post" / "pk").mkdir(parents=True)
    (rdir / "post" / "pk" / "summary.json").write_text('{"ipc": 1.5}')
    (rdir / "post" / "pk" / "report.md").write_text("# report\n")

    ref = f"r/x/{rs.name}"
    short = service.result_get(ref)
    assert short["post"]["pk"] == {"ipc": 1.5}                     # decision-relevant, kept
    assert short["failures"] == [{"task": "run", "point": "bad", "rc": 1,
                                  "failure": "assert-mismatch"}]
    assert short["provenance"]["submodules"] == 2 and short["provenance"]["submodules_dirty"] == ["a"]
    names = {"params": {"run": {"benchmarks": [f"b{i}" for i in range(50)]}}}
    service.digest_run_params(names)                              # names -> count
    assert names["params"]["run"]["benchmarks"] == 50
    for gone in ("recipes", "overrides", "reports"):
        assert gone not in short
    assert "--recipes" in short["sections_hint"] and len(json.dumps(short)) < 4096

    one = service.result_get(ref, sections=["recipes"])
    assert one["recipes"]["run"]["W"] == 1 and "overrides" not in one

    full = service.result_get(ref, detail="full")
    assert full["recipes"]["run"]["benchmarks"][0] == "b0" and full["reports"]["pk"] == "# report\n"
    assert full["provenance"]["submodules"]["a"]["head"] == "1"

    with pytest.raises(ValueError, match="unknown section"):
        service.result_get(ref, sections=["nope"])


def test_node_context_summary_digests_runsets_and_ancestors(root):
    from featx import context
    from featx.model import Task

    Tree().add("p/child", parents=True)
    Tree().update("p", comment="x" * 500, hypothesis="parent hypothesis")
    entry = Tree().get("p/child")
    rs, _ = Tree().new_runset(entry, purpose="p")
    rs.recipes = {"run": {"W": 1}}
    rs.overrides = {"run": {"W": 1}}
    rs.provenance = {"super": {"head": "a"}}
    task = Task(id="T1", kind="run", name="run", node_id=entry.node.id, runset_id=rs.id,
                runset_name=rs.name)
    taskstore.save(task)
    rs.tasks = ["T1"]
    Tree().save_runset(entry, rs)

    full = service.node_context("p/child", detail="full")
    short = service.node_context("p/child")                     # summary is the default
    assert {"params", "code", "overrides", "problem_log_tail"} <= set(full["runsets"][-1])
    assert set(short["runsets"][-1]) == set(context.RUNSET_DIGEST_KEYS) | {"differing_values"}
    assert short["runsets"][-1]["review"] and short["runsets"][-1]["tasks"]   # decision parts stay
    assert full["ancestors"][-1]["comment"] == "x" * 500
    assert "comment" not in short["ancestors"][-1] and short["ancestors"][-1]["hypothesis"]
    assert short["recipes"] and "git" in short                   # the reason to use --full at all
    assert len(json.dumps(short["runsets"][-1])) < len(json.dumps(full["runsets"][-1]))
    assert len(json.dumps(short["ancestors"])) < len(json.dumps(full["ancestors"]))

    brief = service.node_brief("p/child")                             # concise view has no recipes
    assert "recipes" not in brief
    with_recipes = service.node_brief("p/child", recipes=True)        # the --recipes shortcut
    assert with_recipes["recipes"]["run"]["W"] == 20000000
    assert set(with_recipes["recipes"]) == {"build", "run", "post"}
