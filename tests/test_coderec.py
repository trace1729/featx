"""Code records (HEAD + saved patch, nothing committed), the code API and drift handling."""

import subprocess
from pathlib import Path

import pytest

from featx import coderec, provenance, service, store
from featx.model import GitInfo, SubWorktree
from featx.sched import executor
from featx.tree import Tree, TreeError

from test_core import _git
from test_sched_local import make_node, run_until, sched


def _git_out(*args, cwd=None, env=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, check=True, env=env).stdout


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


def _objects(repo) -> int:
    out = _git_out("count-objects", "-v", cwd=repo)
    vals = dict(line.split(": ") for line in out.splitlines())
    return int(vals["count"]) + int(vals["in-pack"])


def test_record_saves_patches_and_omits_build_dirs(root, tmp_path):
    repo = _repo_with_submodule(tmp_path)
    (repo / "deps" / "sub" / "s.txt").write_text("s changed\n")     # tracked change in the submodule
    (repo / "a.txt").write_text("one\ntwo\n")
    (repo / "u.txt").write_text("new\n")
    (repo / "tool.sh").write_text("#!/bin/sh\n")
    (repo / "tool.sh").chmod(0o755)
    (repo / "big.bin").write_bytes(b"x" * (2 << 20))
    (repo / ".gitignore").write_text("ignored.txt\n")
    (repo / "ignored.txt").write_text("ignored\n")
    build = repo / "build-vlt"                                      # a stray build output dir
    build.mkdir()
    for i in range(60):
        (build / f"f{i}.cpp").write_text(f"{i}\n")
    (repo / "src").mkdir()
    (repo / "src" / "new.scala").write_text("object X\n")          # a small new source dir is kept
    index_before = (repo / ".git" / "index").read_bytes()
    objects_before = {r: _objects(r) for r in (repo, repo / "deps" / "sub")}

    dest = tmp_path / "code"
    rec = coderec.record(repo, dest, super_base="main")
    assert (repo / ".git" / "index").read_bytes() == index_before   # the user's index is untouched
    assert {r: _objects(r) for r in objects_before} == objects_before   # and nothing is written
    assert not _git_out("for-each-ref", "refs/featx/", cwd=repo).strip()
    sup = rec["super"]
    assert sup["head"] == _git_out("rev-parse", "HEAD", cwd=repo).strip() and sup["dirty"]
    assert set(sup["untracked"]) == {"u.txt", "tool.sh", ".gitignore", "src/new.scala"}
    omitted = {o["path"]: o["reason"] for o in sup["omitted"]}
    assert set(omitted) == {"build-vlt/", "big.bin"} and "50 files" in omitted["build-vlt/"]
    assert sup["patch"] == "super.patch" and sup["files"] == 5 and sup["added"] >= 5
    patch = (dest / "super.patch").read_bytes()
    assert b"build-vlt" not in patch and b"a/ignored.txt" not in patch and b"+ignored\n" not in patch
    subinfo = rec["submodules"]["deps/sub"]
    assert subinfo["base"] and subinfo["patch"] == "deps__sub.patch" and subinfo["files"] == 1

    # the saved patch rebuilds the exact tree in a throwaway object dir (executable bit included)
    with coderec.Trees(repo) as trees:
        tree = trees.tree(sup["head"], patch)
        env = trees.env
        assert _git_out("show", f"{tree}:u.txt", cwd=repo, env=env) == "new\n"
        assert _git_out("show", f"{tree}:a.txt", cwd=repo, env=env) == "one\ntwo\n"
        assert "100755 blob" in _git_out("ls-tree", tree, "tool.sh", cwd=repo, env=env)
    assert {r: _objects(r) for r in objects_before} == objects_before

    # a clean checkout records just the HEAD and leaves no patch
    for p in ("a.txt",):
        _git("checkout", "--", p, cwd=repo)
    _git("checkout", "--", "s.txt", cwd=repo / "deps" / "sub")
    for p in ("u.txt", "tool.sh", "big.bin", ".gitignore", "ignored.txt"):
        (repo / p).unlink()
    import shutil
    shutil.rmtree(build)
    shutil.rmtree(repo / "src")
    clean = coderec.record(repo, dest, super_base="main")
    assert not clean["super"]["dirty"] and clean["super"]["patch"] is None
    assert not dest.exists() or not any(dest.iterdir())


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


def _save_record(wt, rs, base="main"):
    entry, rs = Tree().find_runset(f"c/{rs.name}")
    rec = coderec.record(wt, entry.dir / rs.name / "code", super_base=base)
    rs.provenance = {"code": rec}
    Tree().save_runset(entry, rs)
    return rec


def test_code_api_against_base_prev_worktree_and_runset(root, tmp_path):
    repo, wt = _node_worktree(root, tmp_path)
    tree = Tree()
    rs1, _ = tree.new_runset(tree.get("c"))
    (wt / "a.txt").write_text("one\ntwo\n")
    (wt / "u1.txt").write_text("u1\n")
    rec1 = _save_record(wt, rs1)
    ref1 = f"c/{rs1.name}"
    objects_before = _objects(repo)

    code = service.runset_code(ref1, "base")
    assert code["legacy"] is False and code["source_runset"] is None
    r = code["repos"][""]
    files = {f["path"]: f for f in r["files"]}
    assert set(files) == {"a.txt", "u1.txt"} and all(f["uncommitted"] for f in files.values())
    assert code["record"][""]["head"] == rec1["super"]["head"] and code["record"][""]["files"] == 2
    assert code["total"]["files"] == 2 and r["ahead"] == 0
    assert "+two" in service.runset_code_diff(ref1, "base", "", "a.txt")["diff"]
    assert "+u1" in service.runset_code_diff(ref1, "base", "", "u1.txt")["diff"]

    # against the worktree as it is now (including a brand new untracked file)
    (wt / "a.txt").write_text("one\ntwo\nthree\n")
    (wt / "u2.txt").write_text("u2\n")
    now = service.runset_code(ref1, "worktree")
    files = {f["path"]: f for f in now["repos"][""]["files"]}
    assert set(files) == {"a.txt", "u2.txt"} and files["a.txt"]["added"] == 1
    diff = service.runset_code_diff(ref1, "worktree", "", "a.txt")
    assert "+three" in diff["diff"]
    assert "+u2" in service.runset_code_diff(ref1, "worktree", "", "u2.txt")["diff"]
    with pytest.raises(TreeError):
        service.runset_code_diff(ref1, "worktree", "", "missing.txt")

    # a second runset, compared with the first as `prev` and by explicit ref; the first one's
    # uncommitted edits are now committed, which is not a difference in content
    assert _objects(repo) == objects_before      # views never write to the repo
    _git("add", "-A", cwd=wt)
    _git("commit", "-q", "-m", "two", cwd=wt)
    objects_before = _objects(repo)
    rs2, _ = tree.new_runset(Tree().get("c"))
    (wt / "a.txt").write_text("one\ntwo\nthree\nfour\n")
    _save_record(wt, rs2)
    ref2 = f"c/{rs2.name}"
    prev = service.runset_code(ref2, "prev")
    assert prev["compared_runset"] == ref1 and prev["against"] == "prev"
    files = {f["path"]: f for f in prev["repos"][""]["files"]}
    assert set(files) == {"a.txt", "u2.txt"}
    assert files["a.txt"]["added"] == 2 and files["a.txt"]["uncommitted"] and not files["u2.txt"]["uncommitted"]
    assert prev["repos"][""]["ahead"] == 1 and prev["repos"][""]["commits"][0]["subject"] == "two"
    other = service.runset_code(ref2, ref1)
    assert other["against"] == "runset" and other["compared_runset"] == ref1
    assert service.runset_code(ref1, "base")["compared_runset"] is None
    with pytest.raises(TreeError):
        service.runset_code(ref2, "c/20990101-9")
    assert _objects(repo) == objects_before      # views never write to the repo

    # a saved patch that was lost: committed code only, reported per repo
    entry, rs2b = Tree().find_runset(ref2)
    (entry.dir / rs2b.name / "code" / "super.patch").unlink()
    lost = service.runset_code(ref2, "base")["repos"][""]
    assert "missing" in lost["patch_error"] and {f["path"] for f in lost["files"]} == {"a.txt", "u1.txt", "u2.txt"}

    # a runset that saved src-patches but recorded no HEAD shows them raw
    rs3, rdir = tree.new_runset(Tree().get("c"))
    (rdir / "src-patches").mkdir()
    (rdir / "src-patches" / "super.patch").write_text("diff --git a/a.txt b/a.txt\n")
    legacy = service.runset_code(f"c/{rs3.name}", "base")
    assert legacy["legacy"] is True and legacy["patches"]["super.patch"].startswith("diff --git")


def test_code_from_the_runset_that_built_the_bin(root, tmp_path):
    repo, wt = _node_worktree(root, tmp_path)
    tree = Tree()
    rs1, _ = tree.new_runset(tree.get("c"))
    (wt / "a.txt").write_text("one\ntwo\n")
    rec = _save_record(wt, rs1)
    # a run-only runset that used the binary the first one built (--bin marks it imported)
    rs2, rdir = tree.new_runset(Tree().get("c"))
    bindir = rdir / "fake-bin"
    bindir.mkdir()
    (bindir / "emu").write_text("x")
    store.write_json(bindir / "manifest.json", {"key": "k1", "node": "c", "runset": rs1.name})
    rs2.bin = {"key": "k1", "emu": str(bindir / "emu"), "sha256": "", "cached": True, "imported": True}
    Tree().save_runset(Tree().get("c"), rs2)
    out = service.runset_code(f"c/{rs2.name}", "base")
    assert out["source_runset"] == f"c/{rs1.name}"
    assert out["record"][""]["head"] == rec["super"]["head"]
    assert {f["path"] for f in out["repos"][""]["files"]} == {"a.txt"}
    # an imported ext- binary (no builder in its manifest) has no code view
    rs3, rdir3 = tree.new_runset(Tree().get("c"))
    (rdir3 / "ext").mkdir()
    (rdir3 / "ext" / "emu").write_text("x")
    rs3.bin = {"key": "ext-1", "emu": str(rdir3 / "ext" / "emu"), "imported": True}
    Tree().save_runset(Tree().get("c"), rs3)
    assert service.runset_code(f"c/{rs3.name}")["no_code"] is True


def _attach_worktree(ref: str, tmp_path) -> Path:
    """Give an existing node a real git worktree (code records need one)."""
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
    rec0 = rs.provenance["code"]["super"]
    assert rec0["head"] and not rec0["dirty"]

    # the tree moves between materialize and launch: re-record and re-key, drift=after submit
    (wd / "src.txt").write_text("v2\n")
    with s.lock:
        s._prepare_job(s._tree(), task, task.jobs[0])
    assert task.outputs["bincache_key"] != key0
    _, rs = Tree().find_runset(res["runset"])
    assert rs.provenance["drift"] == "after submit"
    assert rs.provenance["code"]["super"]["patch"] == "super.patch"
    assert b"+v2" in (entry.dir / rs.name / "code" / "super.patch").read_bytes()


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


def test_legacy_runsets_diff_by_their_recorded_heads(root, tmp_path):
    """Runsets from before code records compare by the HEADs they recorded at submit plus their
    src-patches (tracked changes at submit)."""
    repo, wt = _node_worktree(root, tmp_path)
    tree = Tree()

    def legacy(head, bin=None):
        rs, rdir = tree.new_runset(Tree().get("c"))
        rs.provenance = {"super": {"head": head, "branch": "c", "dirty": False}}
        (rdir / "src-patches").mkdir()
        if bin:
            rs.bin = bin
        Tree().save_runset(Tree().get("c"), rs)
        return rs, rdir

    head1 = _git_out("rev-parse", "HEAD", cwd=wt).strip()
    rs1, rdir1 = legacy(head1)
    (wt / "a.txt").write_text("one\ntwo\n")
    (wt / "b.txt").write_text("b\n")
    _git("add", ".", cwd=wt)
    _git("commit", "-q", "-m", "two", cwd=wt)
    head2 = _git_out("rev-parse", "HEAD", cwd=wt).strip()
    # the build runset's binary records itself as its builder: it is not "code from" itself
    bindir = tmp_path / "bin2"
    bindir.mkdir()
    (bindir / "emu").write_text("x")
    rs2, _ = legacy(head2, {"key": "k2", "emu": str(bindir / "emu"), "cached": False})
    store.write_json(bindir / "manifest.json", {"key": "k2", "node": "c", "runset": rs2.name})
    ref1, ref2 = f"c/{rs1.name}", f"c/{rs2.name}"

    base = service.runset_code(ref2, "base")
    assert base["legacy"] is True and base["source_runset"] is None and base["record"][""]["head"] == head2
    assert {f["path"] for f in base["repos"][""]["files"]} == {"a.txt", "b.txt"}
    prev = service.runset_code(ref2, "prev")
    assert prev["compared_runset"] == ref1 and prev["compared_legacy"] is True
    assert {f["path"] for f in prev["repos"][""]["files"]} == {"a.txt", "b.txt"}
    assert "+two" in service.runset_code_diff(ref2, ref1, "", "a.txt")["diff"]
    assert service.runset_code(ref1, ref2)["repos"][""]["files"][0]["status"] in "AMD"

    # a run-only runset (recorded HEAD = a later commit) shows the code its binary was built from
    (wt / "a.txt").write_text("one\ntwo\nthree\n")
    _git("commit", "-q", "-am", "three", cwd=wt)
    rs3, _ = legacy(_git_out("rev-parse", "HEAD", cwd=wt).strip(),
                    {"key": "k2", "emu": str(bindir / "emu"), "cached": True})
    run_only = service.runset_code(f"c/{rs3.name}", ref1)
    assert run_only["source_runset"] == ref2 and run_only["record"][""]["head"] == head2
    assert {f["path"] for f in run_only["repos"][""]["files"]} == {"a.txt", "b.txt"}

    # src-patches (taken by `git diff HEAD --binary` at submit) are applied on the recorded HEAD
    (wt / "a.txt").write_text("one\ntwo\nthree\nfour\n")
    rs5, rdir5 = legacy(_git_out("rev-parse", "HEAD", cwd=wt).strip())
    (rdir5 / "src-patches" / "super.patch").write_text(_git_out("diff", "HEAD", "--binary", cwd=wt))
    dirty = service.runset_code(f"c/{rs5.name}", "base")
    files = {f["path"]: f for f in dirty["repos"][""]["files"]}
    assert files["a.txt"]["added"] == 3 and files["a.txt"]["uncommitted"] and not files["b.txt"]["uncommitted"]
    assert dirty["record"][""]["files"] == 1 and dirty["record"][""]["added"] == 1
    _git("checkout", "--", "a.txt", cwd=wt)

    # a recorded HEAD that no longer exists is reported per repo instead of an empty diff
    rs4, _ = legacy("0" * 40)
    gone = service.runset_code(f"c/{rs4.name}", ref1)
    assert "no longer in this repo" in gone["repos"][""]["error"]


def test_legacy_shared_submodule_diffs_against_the_commit_the_base_records(root, tmp_path):
    """A shared submodule has no worktree of its own: its base is the gitlink in the node base."""
    repo = _repo_with_submodule(tmp_path)
    _git("checkout", "-q", "-b", "c", cwd=repo)
    e = Tree().add("c")
    e.node.git = GitInfo(repo=str(repo), base="main", branch="c", worktree=str(repo))
    store.write_yaml(e.dir / "node.yaml", e.node.model_dump())
    subdir = repo / "deps" / "sub"
    (subdir / "s.txt").write_text("s\nmore\n")
    _git("commit", "-q", "-am", "sub change", cwd=subdir)
    tree = Tree()
    rs, _ = tree.new_runset(tree.get("c"))
    rs.provenance = {"super": {"head": _git_out("rev-parse", "HEAD", cwd=repo).strip()},
                     "submodules": {"deps/sub": {"head": _git_out("rev-parse", "HEAD", cwd=subdir).strip(),
                                                 "shared": True},
                                    "deps/new": {"head": "1" * 40, "shared": True}}}
    Tree().save_runset(Tree().get("c"), rs)
    code = service.runset_code(f"c/{rs.name}", "base")
    sub = code["repos"]["deps/sub"]
    assert not sub.get("error") and sub["ahead"] == 1
    assert {f["path"] for f in sub["files"]} == {"s.txt"}
    # a submodule the base does not have yet was added on the node's branch
    assert "not in the node base" in code["repos"]["deps/new"]["error"]
    # ... and when it is checked out, the base view shows its uncommitted changes on its HEAD
    newsub = tmp_path / "newsub"
    _git("init", "-q", "-b", "main", str(newsub), cwd=tmp_path)
    (newsub / "n.txt").write_text("n\n")
    _git("add", "-A", cwd=newsub)
    _git("commit", "-q", "-m", "n", cwd=newsub)
    (newsub / "n.txt").write_text("n\nlocal\n")
    e = Tree().get("c")
    e.node.git.submodule_worktrees = {"deps/new": SubWorktree(repo=str(newsub), base="", branch="main", path=str(newsub))}
    store.write_yaml(e.dir / "node.yaml", e.node.model_dump())
    rs = Tree().find_runset(f"c/{rs.name}")[1]
    sup = {"head": _git_out("rev-parse", "HEAD", cwd=repo).strip(), "dirty": False, "patch": None}
    new_rec = coderec.record(newsub, e.dir / rs.name / "code")["super"]     # saved as super.patch
    rs.provenance = {"code": {"super": sup, "submodules": {"deps/new": new_rec}}}
    Tree().save_runset(e, rs)
    new = service.runset_code(f"c/{rs.name}", "base")["repos"]["deps/new"]
    assert not new.get("error") and "only its uncommitted changes" in new["note"]
    assert [(f["path"], f["added"], f["uncommitted"]) for f in new["files"]] == [("n.txt", 1, True)]
