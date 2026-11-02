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


def test_stage_params_reads_legacy_and_broken_snapshots():
    legacy = {"build": {"type": "gsim-two-step", "mode": "trace"},
              "run": {"mode": "trace", "points": ["ok_1_0.5"]},
              "post": {"steps": {"pk": {"plugin": "perf-keys", "baselines": ["exec-reference"]}}}}
    p = params.stage_params(legacy, {"key": "abc", "cached": False})
    assert p["build"]["tool"] == "gsim" and p["build"]["cache_hit"] is False
    assert p["post"]["pk.plugin"] == "perf-keys"
    assert p["post"]["pk.baselines"] == ["exec-reference"]
    # a run snapshot that cannot be resolved must not raise (read-only check over production data)
    assert params.stage_params({"run": {"mode": "exec", "suite": {"config": "/nope.json"},
                                        "points_file": "/nope.txt"}})["run"]["points"] == 0


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
