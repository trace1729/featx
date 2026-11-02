"""Runset parameters (params.py), recorded overrides and the archive flag (plan 2)."""

import json

import pytest

from featx import context, leaderboard, params, service, store
from featx.tree import Tree, TreeError

from test_sched_local import make_node, run_until, sched


def _two_runsets(fake_emu):
    make_node(fake_emu, ["ok_1_0.5"])
    s = sched()
    first = s.submit("t/sched/x", ["run"], bin_ref=str(fake_emu[0]))
    second = s.submit("t/sched/x", ["run"], bin_ref=str(fake_emu[0]),
                      overrides={"run": {"W": 2000000, "K": None}})
    tree = Tree()
    return s, first, second, tree.find_runset(first["runset"])[1], tree.find_runset(second["runset"])[1]


def test_stage_params_and_differing_keys(root, fake_emu):
    s, first, second, rs1, rs2 = _two_runsets(fake_emu)
    p1 = params.stage_params(rs1.recipes, rs1.bin)
    assert p1["run"]["mode"] == "trace"
    assert p1["run"]["W"] == 1
    assert p1["run"]["points"] == 1 and p1["run"]["benchmarks"] == ["ok"]
    assert p1["build"] == {}      # a run-only submit records no build recipe

    assert rs2.overrides == {"run": {"W": 2000000, "K": None}}
    assert params.overridden_keys(rs2.overrides) == ["run.K", "run.W"]
    p2 = params.stage_params(rs2.recipes, rs2.bin)
    assert p2["run"]["W"] == 2000000 and "K" not in p2["run"]

    differing = params.differing([rs1, rs2])
    assert "run.W" in differing
    # snapshot directories and bincache keys differ per runset but say nothing about the experiment
    assert not [k for k in differing if "runset" in k or "bincache" in k or k.endswith(".emu")]


def test_differing_skips_absent_stages_and_derived_keys():
    """A run-only runset has no build recipe: that is not a difference in every build key (the
    production node tracertl/migrate-kmhv3-kimi/squash listed 47 keys, build.artifact first)."""
    built = {"build": {"tool": "gsim", "artifact": "build/emu", "cores": 64, "type": "tracertl-gsim"},
             "run": {"W": 0, "commands": {"exec": "/a/20261027-1/bin/emu"}, "trace_root": "/t/a"}}
    built2 = {"build": {"tool": "verilator", "artifact": "build/emu", "cores": 64, "type": "tracertl-verilator"},
              "run": {"W": 0, "commands": {"exec": "/b/emu"}, "trace_root": "/t/b"}}
    run_only = {"run": {"W": 20000000, "commands": {"exec": "/c/emu"}, "trace_root": "/t/c", "dump_db": True}}
    assert params.differing([built, built2, run_only]) == ["build.tool", "run.W", "run.dump_db"]
    # one runset with a stage is no difference either
    assert params.differing([built, run_only, {"run": {"W": 20000000}}]) == ["run.W", "run.dump_db"]


def test_node_context_gives_differing_values_and_code_summary(root, fake_emu):
    s, first, second, rs1, rs2 = _two_runsets(fake_emu)
    tree = Tree()
    ctx = context.build(tree, tree.get("t/sched/x"), max_runsets=10)
    assert ctx["params_differing"] == ["run.W"]
    values = [r["differing_values"] for r in ctx["runsets"]]
    assert values == [{"run.W": 1}, {"run.W": 2000000}]
    code = ctx["runsets"][0]["code"]
    assert code["uncommitted"] is None and code["from"] is None and code["bin_emu"] == str(fake_emu[0])


def test_code_summary_of_legacy_and_bin_runsets(tmp_path):
    from types import SimpleNamespace
    entry = SimpleNamespace(rel="a/b", dir=tmp_path)
    legacy = SimpleNamespace(name="20261027-1", recipes={"build": {"tool": "gsim"}}, bin={"key": "k"},
                             provenance={"super": {"head": "7a1cd62e2bbd", "dirty": True}})
    c = context.code_summary(entry, legacy)
    assert c["legacy"] and c["head"] == "7a1cd62e2bbd" and c["dirty"] and c["uncommitted"] is None
    # legacy src-patches are counted from their text
    (tmp_path / "20261027-1" / "src-patches").mkdir(parents=True)
    (tmp_path / "20261027-1" / "src-patches" / "deps__sub.patch").write_text(
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n-a\n+b\n+c\n")
    c = context.code_summary(entry, legacy)
    assert c["uncommitted"] == {"repos": ["deps/sub"], "files": 1, "added": 2, "deleted": 1}
    # a code record: its HEAD plus the stats of the saved patches of the dirty repos
    rec = SimpleNamespace(name="20261102-1", recipes={"build": {}}, bin={},
                          provenance={"code": {"super": {"head": "abc123", "dirty": True, "files": 2, "added": 5,
                                                         "deleted": 1, "omitted": [{"path": "build/"}]},
                                               "submodules": {"s": {"head": "d", "dirty": False}}},
                                      "super": {"head": "h"}})
    c = context.code_summary(entry, rec)
    assert c["head"] == "abc123" and not c["legacy"] and c["omitted"] == 1
    assert c["uncommitted"] == {"repos": [""], "files": 2, "added": 5, "deleted": 1}
    # a run-only runset names the runset that built its binary (bincache manifest)
    (tmp_path / "bc").mkdir()
    store.write_json(tmp_path / "bc" / "manifest.json", {"node": "a/b", "runset": "20261027-1"})
    runonly = SimpleNamespace(name="20261027-5", recipes={"run": {"W": 1}}, provenance={},
                              bin={"key": "bc", "emu": str(tmp_path / "bc" / "emu")})
    assert context.code_summary(entry, runonly)["from"] == "a/b/20261027-1"


def test_stage_params_reads_legacy_and_broken_snapshots():
    legacy = {"build": {"type": "gsim-two-step", "mode": "trace"},
              "run": {"mode": "trace", "points": ["ok_1_0.5"]},
              "post": {"steps": {"pk": {"plugin": "perf-keys", "baselines": ["exec-reference"]}}}}
    p = params.stage_params(legacy, {"key": "abc", "cached": False})
    assert p["build"]["tool"] == "gsim" and p["build"]["cache_hit"] is False
    assert p["post"]["pk.plugin"] == "perf-keys"
    assert p["post"]["pk.baselines"] == ["exec-reference"]
    # a run snapshot that cannot be resolved must not raise (read-only check over production data)
    # ... and leaves the point count out rather than claim 0 points
    assert "points" not in params.stage_params({"run": {"mode": "exec", "suite": {"config": "/nope.json"},
                                                        "points_file": "/nope.txt"}})["run"]


def test_result_get_exposes_recipes_overrides_and_params(root, fake_emu):
    s, first, second, rs1, rs2 = _two_runsets(fake_emu)
    r = service.result_get(second["runset"])
    assert r["overrides"]["run"]["W"] == 2000000
    assert r["recipes"]["run"]["W"] == 2000000
    assert r["params"]["run"]["W"] == 2000000


def test_archive_filters_lists_and_is_refused_while_tasks_are_active(root, fake_emu):
    s, first, second, rs1, rs2 = _two_runsets(fake_emu)
    run_task = s.get(first["tasks"][0]["id"])
    with pytest.raises(TreeError, match="active"):
        service.runset_archive(first["runset"], note="duplicate")

    s.cancel(run_task.id)
    out = service.runset_archive(first["runset"], note="duplicate of the next one")
    assert out["archived"] and out["archive_note"] == "duplicate of the next one"
    assert [r["ref"] for r in service.runsets("t/sched/x")] == [second["runset"]]
    assert [r["ref"] for r in service.runsets("t/sched/x", include_archived=True)] == \
        [first["runset"], second["runset"]]
    ctx = service.node_context("t/sched/x")
    assert ctx["archived_count"] == 1 and [r["ref"] for r in ctx["runsets"]] == [second["runset"]]
    assert [r["ref"] for r in service.node_context("t/sched/x", include_archived=True)["runsets"]] == \
        [first["runset"], second["runset"]]

    # the node's tree row hides an archived latest runset
    rows = {r["path"]: r for r in service.tree_rows(Tree())}
    assert rows["t/sched/x"]["archived_count"] == 1

    # unarchive brings it back
    back = service.runset_archive(first["runset"], archived=False)
    assert not back["archived"] and back["archive_note"] == ""
    assert len(service.runsets("t/sched/x")) == 2


def test_leaderboard_skips_archived_runsets(root, fake_emu):
    make_node(fake_emu, ["ok_1_0.5"])
    s = sched()
    first = s.submit("t/sched/x", ["run"], bin_ref=str(fake_emu[0]))
    second = s.submit("t/sched/x", ["run"], bin_ref=str(fake_emu[0]))
    for res in (first, second):
        entry, rs = Tree().find_runset(res["runset"])
        rs.archived = res["runset"] == first["runset"]
        Tree().save_runset(entry, rs)
        store.write_json(entry.dir / rs.name / "post" / "pk" / "summary.json",
                         {"reference": "exec-reference", "block": 2, "points": 1, "common_points": 1,
                          "self.mean_abs_err_pct": 1.0})
    board = leaderboard.build(metric="pk.self.mean_abs_err_pct")
    assert [r["runset"] for r in board["rows"]] == [second["runset"]]
    assert {r["runset"] for r in leaderboard.build(metric="pk.self.mean_abs_err_pct",
                                                   include_archived=True)["rows"]} == \
        {first["runset"], second["runset"]}


def test_cli_archive_command(root, fake_emu):
    from typer.testing import CliRunner
    from featx import cli
    s, first, second, rs1, rs2 = _two_runsets(fake_emu)
    s.cancel(first["tasks"][0]["id"])
    runner = CliRunner()
    res = runner.invoke(cli.app, ["runset", "archive", first["runset"], "-m", "old"])
    assert res.exit_code == 0, res.output
    assert Tree().find_runset(first["runset"])[1].archived
    res = runner.invoke(cli.app, ["runset", "archive", first["runset"], "--undo"])
    assert res.exit_code == 0, res.output
    assert not Tree().find_runset(first["runset"])[1].archived
