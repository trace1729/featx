"""Code snapshots, the code API and drift handling (plan 3)."""

import subprocess
from pathlib import Path

import pytest

from featx import provenance, service, snapshots, store
from featx.model import GitInfo
from featx.sched import executor
from featx.tree import Tree, TreeError

from test_core import _git
from test_sched_local import make_node, run_until, sched


def _git_out(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, check=True).stdout


def _repo_with_submodule(tmp_path):
    repo, sub = tmp_path / "repo", tmp_path / "sub"
    _git("init", "-q", "-b", "main", str(repo))
    _git("init", "-q", "-b", "main", str(sub))
    (sub / "s.txt").write_text("s\n")
    _git("add", ".", cwd=sub)
    _git("commit", "-q", "-m", "sub", cwd=sub)
    (repo / "a.txt").write_text("one\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    _git("-c", "protocol.file.allow=always", "submodule", "add", str(sub), "deps/sub", cwd=repo)
    _git("commit", "-q", "-m", "add sub", cwd=repo)
    return repo


def test_snapshot_ref_submodule_untracked_and_big_file(root, tmp_path):
    repo = _repo_with_submodule(tmp_path)
    (repo / "deps" / "sub" / "s.txt").write_text("s changed\n")     # tracked change in the submodule
    (repo / "a.txt").write_text("one\ntwo\n")
    (repo / "u.txt").write_text("new\n")
    (repo / "big.bin").write_bytes(b"x" * (9 << 20))
    (repo / ".gitignore").write_text("ignored.txt\n")
    (repo / "ignored.txt").write_text("ignored\n")
    index_before = (repo / ".git" / "index").read_bytes()

    snap = snapshots.worktree_snapshot(repo, "featx/runsets/RS1", super_base="main")
    assert (repo / ".git" / "index").read_bytes() == index_before   # the user's index is untouched
    sup = snap["super"]
    assert sup["commit"] != sup["head"] and not sup["clean"]
    assert "u.txt" in sup["untracked"] and ".gitignore" in sup["untracked"]
    assert sup["skipped"] == [{"path": "big.bin", "reason": "larger than 8 MiB"}]
    assert _git_out("show", f"{sup['commit']}:a.txt", cwd=repo) == "one\ntwo\n"
    assert _git_out("show", f"{sup['commit']}:u.txt", cwd=repo) == "new\n"
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sup['commit']}:big.bin"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert _git_out("rev-parse", "refs/featx/runsets/RS1", cwd=repo).strip() == sup["commit"]

    subinfo = snap["submodules"]["deps/sub"]
    assert subinfo["commit"] != subinfo["head"] and subinfo["base"]
    assert _git_out("show", f"{subinfo['commit']}:s.txt", cwd=repo / "deps" / "sub") == "s changed\n"
    assert _git_out("rev-parse", "refs/featx/runsets/RS1", cwd=repo / "deps" / "sub").strip() == subinfo["commit"]

    # a clean tree points the ref at HEAD instead of creating an empty commit
    (repo / "big.bin").unlink()
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "commit all", cwd=repo)
    (repo / "deps" / "sub" / "s.txt").write_text("s\n")     # leave the submodule clean as well
    clean = snapshots.snapshot(repo, "featx/runsets/RS2")
    assert clean["clean"] and clean["commit"] == clean["head"]
    assert _git_out("rev-parse", "refs/featx/runsets/RS2", cwd=repo).strip() == clean["head"]


def _node_worktree(root, tmp_path):
    repo, wt = tmp_path / "repo", tmp_path / "wt" / "c"
    _git("init", "-q", "-b", "main", str(repo))
    (repo / "a.txt").write_text("one\n")
    (repo / ".gitmodules").write_text("")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    _git("worktree", "add", "-q", "-b", "c", str(wt), "main", cwd=repo)
    e = Tree().add("c")
    e.node.git = GitInfo(repo=str(repo), base="main", branch="c", worktree=str(wt))
    store.write_yaml(e.dir / "node.yaml", e.node.model_dump())
    return repo, wt


def _save_snapshot(wt, rs, base="main"):
    snap = snapshots.worktree_snapshot(wt, f"featx/runsets/{rs.id}", super_base=base)
    entry, rs = Tree().find_runset(f"c/{rs.name}")
    rs.provenance = {"snapshot": snap}
    Tree().save_runset(entry, rs)
    return snap


def test_code_api_against_base_prev_worktree_and_runset(root, tmp_path):
    repo, wt = _node_worktree(root, tmp_path)
    tree = Tree()
    rs1, _ = tree.new_runset(tree.get("c"))
    (wt / "a.txt").write_text("one\ntwo\n")
    (wt / "u1.txt").write_text("u1\n")
    snap1 = _save_snapshot(wt, rs1)
    ref1 = f"c/{rs1.name}"

    code = service.runset_code(ref1, "base")
    assert code["legacy"] is False and code["source_runset"] is None
    r = code["repos"][""]
    assert {f["path"] for f in r["files"]} == {"a.txt", "u1.txt"}
    assert code["snapshot"]["super"]["commit"] == snap1["super"]["commit"]
    assert code["total"]["files"] == 2

    # against the worktree as it is now (including a brand new untracked file)
    (wt / "a.txt").write_text("one\ntwo\nthree\n")
    (wt / "u2.txt").write_text("u2\n")
    now = service.runset_code(ref1, "worktree")
    files = {f["path"]: f for f in now["repos"][""]["files"]}
    assert files["a.txt"]["added"] == 1
    assert now["repos"][""]["untracked"] == ["u2.txt"]
    diff = service.runset_code_diff(ref1, "worktree", "", "a.txt")
    assert "+three" in diff["diff"]
    assert "+u2" in service.runset_code_diff(ref1, "worktree", "", "u2.txt")["diff"]
    with pytest.raises(TreeError):
        service.runset_code_diff(ref1, "worktree", "", "missing.txt")

    # a second runset, compared with the first as `prev` and by explicit ref
    _git("commit", "-q", "-am", "two", cwd=wt)
    rs2, _ = tree.new_runset(Tree().get("c"))
    (wt / "a.txt").write_text("one\ntwo\nthree\nfour\n")
    _save_snapshot(wt, rs2)
    ref2 = f"c/{rs2.name}"
    prev = service.runset_code(ref2, "prev")
    assert prev["compared_runset"] == ref1 and prev["against"] == "prev"
    # u2.txt exists in this snapshot but not in the previous one, so it is part of the difference
    assert {f["path"] for f in prev["repos"][""]["files"]} == {"a.txt", "u2.txt"}
    other = service.runset_code(ref2, ref1)
    assert other["against"] == "runset" and other["compared_runset"] == ref1
    assert {f["path"]: f["added"] for f in other["repos"][""]["files"]}["a.txt"] == 2
    assert service.runset_code(ref1, "base")["compared_runset"] is None
    with pytest.raises(TreeError):
        service.runset_code(ref2, "c/20990101-9")

    # legacy runset: provenance.head plus the saved tracked patches
    rs3, rdir = tree.new_runset(Tree().get("c"))
    rs3.provenance = {"super": {"head": "abc123"}}
    (rdir / "src-patches").mkdir()
    (rdir / "src-patches" / "super.patch").write_text("diff --git a/a.txt b/a.txt\n")
    Tree().save_runset(Tree().get("c"), rs3)
    legacy = service.runset_code(f"c/{rs3.name}", "base")
    assert legacy["legacy"] is True and legacy["head"] == "abc123"
    assert legacy["patches"]["super.patch"].startswith("diff --git")


def test_code_from_the_runset_that_built_the_bin(root, tmp_path):
    repo, wt = _node_worktree(root, tmp_path)
    tree = Tree()
    rs1, _ = tree.new_runset(tree.get("c"))
    (wt / "a.txt").write_text("one\ntwo\n")
    snap = _save_snapshot(wt, rs1)
    # a run-only runset that used the binary the first one built: no snapshot of its own
    rs2, rdir = tree.new_runset(Tree().get("c"))
    bindir = rdir / "fake-bin"
    bindir.mkdir()
    (bindir / "emu").write_text("x")
    store.write_json(bindir / "manifest.json", {"key": "k1", "node": "c", "runset": rs1.name})
    rs2.bin = {"key": "k1", "emu": str(bindir / "emu"), "sha256": "", "cached": True}
    Tree().save_runset(Tree().get("c"), rs2)
    out = service.runset_code(f"c/{rs2.name}", "base")
    assert out["source_runset"] == f"c/{rs1.name}"
    assert out["snapshot"]["super"]["commit"] == snap["super"]["commit"]
    assert {f["path"] for f in out["repos"][""]["files"]} == {"a.txt"}


def _attach_worktree(ref: str, tmp_path) -> Path:
    """Give an existing node a real git worktree (build snapshots need one)."""
    repo, wt = tmp_path / "wtrepo", tmp_path / "node-wt"
    _git("init", "-q", "-b", "main", str(repo))
    (repo / "src.txt").write_text("v1\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    branch = ref.replace("/", "-")
    _git("worktree", "add", "-q", "-b", branch, str(wt), "main", cwd=repo)
    e = Tree().get(ref)
    e.node.git = GitInfo(repo=str(repo), base="main", branch=branch, worktree=str(wt))
    store.write_yaml(e.dir / "node.yaml", e.node.model_dump())
    return wt


def test_build_relaunch_and_launch_rekey_with_drift(root, fake_emu, tmp_path):
    make_node(fake_emu, ["ok_1_0.5"])
    wd = _attach_worktree("t/sched/x", tmp_path)
    service.recipe_set("t/sched/x", "build", {"type": "script", "workdir": str(wd), "cores": 1,
                                              "mem_gb": 1, "script": "true"})
    s = sched()
    res = s.submit("t/sched/x", ["build"])
    task = s.tasks[res["tasks"][0]["id"]]
    with s.lock:
        s._materialize(s._tree(), task)
    key0 = task.outputs["bincache_key"]
    entry, rs = Tree().find_runset(res["runset"])
    snap0 = rs.provenance["snapshot"]["super"]["commit"]
    assert snap0

    # the tree moves between materialize and launch: re-snapshot and re-key, drift=after submit
    (wd / "src.txt").write_text("v2\n")
    with s.lock:
        s._prepare_job(s._tree(), task, task.jobs[0])
    assert task.outputs["bincache_key"] != key0
    _, rs = Tree().find_runset(res["runset"])
    assert rs.provenance["drift"] == "after submit"
    assert rs.provenance["snapshot"]["super"]["commit"] != snap0


def test_a_build_whose_tree_changes_during_compile_is_not_cached(root, fake_emu, tmp_path):
    emu = fake_emu[0]
    make_node(fake_emu, ["ok_1_0.5"])
    wd = _attach_worktree("t/sched/x", tmp_path)
    service.recipe_set("t/sched/x", "build", {"type": "script", "workdir": str(wd), "cores": 1, "mem_gb": 1,
                                              "script": f"echo changed >> src.txt; "
                                                        f"mkdir -p build && cp {emu} build/emu"})
    s = sched()
    res = s.submit("t/sched/x", ["build"])
    task = s.tasks[res["tasks"][0]["id"]]
    run_until(s, lambda: task.state in ("succeeded", "failed"))
    assert task.state == "succeeded", [j.failure for j in task.jobs]
    key = task.outputs["bincache_key"]
    entry, rs = Tree().find_runset(res["runset"])
    assert rs.provenance["drift"] == "during build"
    assert rs.bin["key"] is None and rs.bin["drift"] == "during build"
    assert provenance.bincache_lookup(key) is None
    assert (entry.dir / rs.name / "bin" / "emu").exists()


def test_rm_drop_worktrees_drops_the_runset_refs(root, tmp_path):
    repo, wt = _node_worktree(root, tmp_path)
    tree = Tree()
    rs, _ = tree.new_runset(tree.get("c"))
    _save_snapshot(wt, rs)
    assert _git_out("rev-parse", f"refs/featx/runsets/{rs.id}", cwd=repo).strip()
    report = service.node_rm("c", force=True, drop_worktrees=True)
    assert rs.id in report.get("refs", [])
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"refs/featx/runsets/{rs.id}"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
