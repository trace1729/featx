"""Checkpoint catalog (all layouts, multi-hart / broken sets, weight rounding, cache) and
run.checkpoint: point resolution, images, refusals, provenance, leaderboard class, post
ckpt_mismatch, and an exec run end to end with the fake emu."""

import json
import os
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from featx import ckpt, cli, config, leaderboard, points, presets, service, store
from featx.post import plugins
from featx.tree import Tree

from conftest import update_layer
from test_sched_local import run_until, sched


def _touch(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _json(p: Path, data) -> None:
    _touch(p, json.dumps(data))


def test_exec_only_never_opens_trace_manifest(sets, root, tmp_path, monkeypatch):
    update_layer(root, trace_enabled=False, workloads={"exec-only": {
        "checkpoint": "spec06_modern_a", "manifest": str(tmp_path / "missing.tsv"),
    }})

    def no_manifest(*args):
        pytest.fail("exec-only run opened a trace manifest")

    monkeypatch.setattr(presets, "manifest", no_manifest)
    spec = presets.lower_run({"workload": "exec-only", "commands": {"exec": "echo ${point.checkpoint}"}}, None)
    names = points.resolve(spec, strict=True)
    assert names
    assert presets.point_vars(spec, names[0])["point.checkpoint"].endswith(".zstd")


@pytest.fixture
def sets(root, tmp_path):
    """Two roots of synthetic sets, one of each layout plus multi-hart / broken ones."""
    r1, r2 = tmp_path / "ck1", tmp_path / "ck2"
    m = r1 / "spec06_modern_a"                      # modern: full-precision weights in the json
    _json(m / "json" / "checkpoints_all.json", {
        "ok": {"insts": "1000", "points": {"1": "0.5", "2": "0.4123456"}},
        "gcc_166": {"insts": "50", "points": {"5": "1", "6": "0.25"}}})     # 6: no image
    _touch(m / "checkpoint" / "ok" / "1" / "_1_0.500000_.zstd")
    _touch(m / "checkpoint" / "ok" / "2" / "_2_0.412346_memory_.zstd")
    _touch(m / "checkpoint" / "ok" / "9" / "_9_0.000010_.zstd")               # extra: not in the json
    _touch(m / "checkpoint" / "gcc_166" / "5" / "_5_1.000000_.zstd")
    _json(m / "manifest" / "ok.json", {"multihart": False})
    lg = r1 / "spec17_legacy0_b"                    # legacy0
    _json(lg / "checkpoint-0-0-0" / "cluster-0-0.json", {"xz": {"insts": "7", "points": {"3": "0.25"}}})
    _touch(lg / "checkpoint-0-0-0" / "xz" / "3" / "_3_0.25_.zstd")
    tc = r1 / "spec17_take_c"                       # take_cpt flat, weights only
    _json(tc / "simpoint_summary.json", {"xz": {"7": "0.333333"}})
    _touch(tc / "take_cpt" / "xz_7_0.333333" / "0" / "_7_.gz")
    nest = r1 / "spec06_nested_d"                   # take_cpt nested + the only json under json/
    _json(nest / "json" / "o3_spec.json", {"mcf": {"4": "0.1"}})
    _touch(nest / "take_cpt" / "mcf" / "4" / "_4_0.1_.gz")
    mh = r1 / "spec06_2harts_e"                     # multi-hart by name (no manifest)
    _json(mh / "json" / "checkpoints_all.json", {"ok": {"insts": "1", "points": {"1": "1"}}})
    _touch(mh / "checkpoint" / "ok" / "1" / "_1_1.000000_.zstd")
    mf = r1 / "spec06_mh_manifest_f"                # multi-hart by manifest
    _json(mf / "json" / "checkpoints_all.json", {"ok": {"insts": "1", "points": {"1": "1"}}})
    _touch(mf / "checkpoint" / "ok" / "1" / "_1_1.000000_.zstd")
    _json(mf / "manifest" / "ok.json", {"multihart": True})
    br = r1 / "spec06_broken_g"                     # dangling take_cpt link
    br.mkdir(parents=True)
    os.symlink(tmp_path / "nowhere", br / "take_cpt")
    (r1 / "notaset").mkdir()
    dup = r2 / "spec06_modern_a"                    # shadowed by the first root
    _touch(dup / "checkpoint" / "ok" / "1" / "_1_0.5_.zstd")
    update_layer(root, checkpoints={"roots": [str(r1), str(r2), str(tmp_path / "nosuchroot")]})
    return r1


def test_catalog_layouts(sets):
    rows = {r["name"]: r for r in service.checkpoints()}
    assert set(rows) == {"spec06_modern_a", "spec17_legacy0_b", "spec17_take_c", "spec06_nested_d",
                         "spec06_2harts_e", "spec06_mh_manifest_f", "spec06_broken_g"}
    assert rows["spec06_modern_a"]["path"] == str(sets / "spec06_modern_a")      # first root wins
    a = ckpt.get("spec06_modern_a")
    assert a["layout"] == "modern" and a["spec"] == "06" and a["has_insts"] and not a["restorer_needed"]
    assert sorted(a["points"]) == ["gcc_166_5_1.000000", "ok_1_0.500000", "ok_2_0.412346"]
    p = a["points"]["ok_2_0.412346"]
    assert p["weight"] == "0.4123456" and p["image"].endswith("ok/2/_2_0.412346_memory_.zstd")
    assert (a["missing"], a["extra"], a["benches"]) == (1, 1, 2) and a["insts"] == {"ok": "1000", "gcc_166": "50"}
    b = ckpt.get("spec17_legacy0_b")
    assert b["layout"] == "legacy0" and list(b["points"]) == ["xz_3_0.250000"] and b["insts"] == {"xz": "7"}
    c = ckpt.get("spec17_take_c")
    assert c["layout"] == "take_cpt" and not c["has_insts"] and c["restorer_needed"]
    assert c["points"]["xz_7_0.333333"]["image"].endswith("take_cpt/xz_7_0.333333/0/_7_.gz")
    d = ckpt.get("spec06_nested_d")
    assert d["layout"] == "take_cpt" and d["json"].endswith("o3_spec.json") and list(d["points"]) == ["mcf_4_0.100000"]
    assert rows["spec06_2harts_e"]["multihart"] and rows["spec06_mh_manifest_f"]["multihart"]
    assert not rows["spec06_2harts_e"]["runnable"] and rows["spec06_modern_a"]["runnable"]
    assert "dangling symlink" in rows["spec06_broken_g"]["error"] and not rows["spec06_broken_g"]["runnable"]
    assert [r["name"] for r in service.checkpoints(spec="17")] == ["spec17_legacy0_b", "spec17_take_c"]
    assert {r["name"] for r in service.checkpoints(all_sets=False)} == {
        "spec06_modern_a", "spec17_legacy0_b", "spec17_take_c", "spec06_nested_d"}
    with pytest.raises(ckpt.CkptError, match="multi-hart"):
        ckpt.runnable("spec06_mh_manifest_f")
    with pytest.raises(ckpt.CkptError, match="dangling"):
        ckpt.runnable("spec06_broken_g")
    with pytest.raises(ckpt.CkptError, match="similar: spec06_modern_a"):
        ckpt.find("modern_a")
    assert ckpt.get(str(sets / "spec06_nested_d"))["name"] == "spec06_nested_d"    # a path works too
    show = service.checkpoint_show("spec06_modern_a", bench="gcc")
    assert list(show["points"]) == ["gcc_166_5_1.000000"] and show["insts"] == {"gcc_166": "50"}
    with pytest.raises(KeyError, match="no benchmark 'mcf'"):
        service.checkpoint_show("spec06_modern_a", bench="mcf")


def test_index_cache_rebuilds_on_change(sets):
    d = sets / "spec17_take_c"
    first = ckpt.index(d)
    assert list(ckpt.cache_dir().glob("spec17_take_c-*.json"))
    ckpt._memo.clear()
    assert ckpt.index(d) == first                                   # from the cache file
    _json(d / "simpoint_summary.json", {"xz": {"7": "0.333333", "8": "0.5"}})
    _touch(d / "take_cpt" / "xz_8_0.500000" / "0" / "_8_.gz")
    assert sorted(ckpt.index(d)["points"]) == ["xz_7_0.333333", "xz_8_0.500000"]


def test_cli_ckpt(sets):
    r = CliRunner().invoke(cli.app, ["ckpt", "list", "--all"])
    assert r.exit_code == 0, r.output
    assert "spec06_modern_a" in r.output and "multi-hart (not runnable)" in r.output and "dangling" in r.output
    r = CliRunner().invoke(cli.app, ["ckpt", "list", "--spec", "06"])
    assert "spec17" not in r.output and "spec06_2harts_e" not in r.output
    r = CliRunner().invoke(cli.app, ["ckpt", "show", "spec06_modern_a", "--bench", "ok"])
    assert r.exit_code == 0 and "ok_2_0.412346" in r.output and "0.4123456" in r.output
    r = CliRunner().invoke(cli.app, ["ckpt", "show", "nosuch"])
    assert r.exit_code == 1 and "unknown checkpoint set" in r.output


def _spec(**kw):
    spec = store.read_yaml(config.DEFAULTS_DIR / "recipes" / "run.yaml")
    spec.update(kw)
    return presets.lower_run(spec, None)


def test_bench_selected():
    assert points.bench_selected("500.perlbench_rate_refrate_00", ["500.perlbench"])
    assert points.bench_selected("gcc_166", ["gcc"]) and points.bench_selected("mcf", ["mcf"])
    assert not points.bench_selected("gcc_166", ["gc"]) and not points.bench_selected("mcfx", ["mcf"])


def test_run_checkpoint_resolution(sets):
    spec = _spec(mode="exec", checkpoint="spec06_modern_a", exec={"diff": None, "gcpt_restorer": "/g"})
    assert spec["checkpoint_set"]["name"] == "spec06_modern_a" and spec["checkpoint_set"]["json_sha256"]
    assert spec["checkpoint_set"]["points"] == 3 and presets.run_checkpoint(spec) == "spec06_modern_a"
    assert points.resolve(spec) == ["gcc_166_5_1.000000", "ok_1_0.500000", "ok_2_0.412346"]
    assert points.resolve({**spec, "benchmarks": ["gcc"]}) == ["gcc_166_5_1.000000"]
    assert points.resolve({**spec, "coverage": 0.5}) == ["gcc_166_5_1.000000", "ok_1_0.500000"]
    with pytest.raises(presets.PresetError, match="not in checkpoint set spec06_modern_a"):
        points.resolve({**spec, "points": ["ok_1_0.5"]})
    name = "ok_2_0.412346"
    ctx = {"point.name": name, **points.parse(name), "emu": "/emu", "W": 1, "I": 2, "args": "",
           **presets.point_vars(spec, name)}
    ctx = {(k if k.startswith(("point.", "emu", "W", "I", "args")) else f"point.{k}"): v for k, v in ctx.items()}
    cmd, inputs = presets.point_command(spec, ctx)
    img = str(sets / "spec06_modern_a" / "checkpoint" / "ok" / "2" / "_2_0.412346_memory_.zstd")
    assert inputs == {"checkpoint": img} and cmd.split(" -W ")[0].rstrip().endswith(f"-i {img} --no-diff")
    gz = _spec(mode="exec", checkpoint="spec17_take_c", exec={"diff": None, "gcpt_restorer": "/g"})
    ctx["point.name"] = "xz_7_0.333333"
    ctx.update(presets.point_vars(gz, "xz_7_0.333333"))
    cmd, _ = presets.point_command(gz, ctx)
    assert cmd.split(" -W ")[0].rstrip().endswith("--no-diff -r /g")


def test_run_checkpoint_refusals_and_implied(sets, nfs_site):
    update_layer(nfs_site, checkpoints={"roots": [str(sets)]})
    with pytest.raises(presets.PresetError, match="multi-hart"):
        _spec(mode="exec", checkpoint="spec06_2harts_e")
    with pytest.raises(presets.PresetError, match="unknown checkpoint set"):
        _spec(mode="exec", checkpoint="spec99_none")
    with pytest.raises(presets.PresetError, match="selects images for exec runs"):
        _spec(mode="trace", workload="spec06", checkpoint="spec06_modern_a")
    with pytest.raises(presets.PresetError, match="perf_trigger"):
        _spec(mode="exec", checkpoint="spec06_modern_a", executor="perf_trigger")
    with pytest.raises(presets.PresetError, match="is spec17 but run.workload=spec06 is spec06"):
        _spec(mode="exec", workload="spec06", checkpoint="spec17_take_c")
    assert _spec(mode="exec", workload="spec06", checkpoint="spec06_modern_a")["checkpoint"] == "spec06_modern_a"
    # without run.checkpoint the workload's template stays and its set is recorded
    spec = _spec(mode="exec", workload="spec17")
    assert spec["checkpoint_set"] == {"name": "spec17_rv64gcb_o2_20m", "implied": True}
    assert presets.checkpoint_index(spec) is None and spec["checkpoint_file"] is None   # the workload layer's
    tr = _spec(mode="trace", workload="spec06", checkpoint="spec06_gcc15_rv64gcb_base_260122")
    assert tr["checkpoint_set"]["implied"]
    # an explicit set without a workload is labelled / scored as the workload of its SPEC version
    ex = _spec(mode="exec", checkpoint="spec17_take_c")
    assert presets.context_workload(ex) == "spec17" and presets.context_workload(spec) == "spec17"
    assert presets.run_checkpoint({"W": 1}) == "spec06_gcc15_rv64gcb_base_260122"   # old runsets
    assert ckpt.insts_path("spec06_modern_a").endswith("checkpoints_all.json")
    assert ckpt.insts_path("spec17_take_c") == ""                    # no insts, no workload traced from it
    assert ckpt.insts_path("spec17_rv64gcb_o2_20m") == presets.insts("spec17")
    # a set given by path (not under the roots) has its insts too
    update_layer(nfs_site, checkpoints={"roots": []})
    assert ckpt.insts_path("spec06_modern_a") == ""
    assert ckpt.insts_path("spec06_modern_a", str(sets / "spec06_modern_a")).endswith("checkpoints_all.json")


def test_leaderboard_class_has_the_checkpoint(root, sets):
    from test_ui_api import _runset
    a = _runset("c/a", W=1, I=2, err=1.0)
    b = _runset("c/b", W=1, I=2, err=2.0)
    t = Tree()
    entry, rs = t.find_runset(b)
    rs.recipes = {"run": {"W": 1, "I": 2, "mode": "exec", "checkpoint": "spec06_modern_a",
                          "checkpoint_set": {"name": "spec06_modern_a", "path": "/x"}}}
    t.save_runset(entry, rs)
    leaderboard._cache.clear()
    classes = leaderboard.build()["classes"]
    assert len(classes) == 2 and {c["checkpoint"] for c in classes} == {"-", "spec06_modern_a"}
    assert any("spec06_modern_a" in c["label"] and "ckpt=spec06_modern_a" in c["key"] for c in classes)


def test_checkpoint_mismatch(root, sets, tmp_path):
    site = tmp_path / "site.yaml"
    site.write_text(yaml.safe_dump({"baselines": {"ref": {"root": str(tmp_path), "checkpoint": "other_set"},
                                                  "same": {"root": str(tmp_path), "checkpoint": "spec06_modern_a"}}}))
    update_layer(root, site=str(site))
    t = Tree()
    params = {"baselines": {"r": "@baseline(ref)", "s": "@baseline(same)"}}
    assert plugins.checkpoint_mismatch(params, "spec06_modern_a", t) == {"ref": "other_set"}
    assert plugins.checkpoint_mismatch(params, "", t) == {}


def test_exec_run_with_a_checkpoint_set(root, fake_emu, tmp_path):
    """A set whose images live under ok/ (the fake emu passes on '/ok/' paths): points from the
    set, insts into points.json, provenance, and a post step flagging the baseline's set."""
    r = tmp_path / "ck"
    s6 = r / "spec06_fake_set"
    _json(s6 / "json" / "checkpoints_all.json", {"ok": {"insts": "123", "points": {"1": "0.5"}}})
    _touch(s6 / "checkpoint" / "ok" / "1" / "_1_0.500000_.zstd")
    site = tmp_path / "site.yaml"
    site.write_text(yaml.safe_dump({"baselines": {"ref": {"root": str(tmp_path / "refroot"), "checkpoint": "older_set"}}}))
    # a layer's checkpoint_file template (as the site's recipe_defaults) must not win over the set
    update_layer(root, checkpoints={"roots": [str(r)]}, site=str(site), recipe_defaults={"run": {
        "checkpoint_file": str(tmp_path / "template" / "${point.bench}" / "${point.cp}" / "_${point.cp}_.zstd")}})
    Tree().add("t/ck", parents=True)
    service.recipe_set("t/ck", "run", {"mode": "exec", "checkpoint": "spec06_fake_set", "W": 1, "I": 300000,
                                       "dramsim3_home": None, "exec": {"diff": None}})
    service.recipe_set("t/ck", "post", {"steps": {"pk": {"plugin": "perf-keys", "baselines": ["ref"]}}})
    s = sched()
    res = s.submit("t/ck", ["run"], bin_ref=str(fake_emu[0]))
    run_until(s, lambda: s.tasks[res["tasks"][0]["id"]].state in ("succeeded", "failed"))
    assert s.tasks[res["tasks"][0]["id"]].state == "succeeded", s.tasks[res["tasks"][0]["id"]].failure
    rdir = Path(res["runset_dir"])
    exp = store.read_yaml(rdir / "experiment.yaml")
    assert exp["recipes"]["run"]["checkpoint_set"]["name"] == "spec06_fake_set"
    assert store.read_json(rdir / "run" / "points.json") == {"ok": {"insts": "123", "points": {"1": "0.5"}}} or \
        "123" in json.dumps(store.read_json(rdir / "run" / "points.json"))
    argv = (rdir / "run" / "ok_1_0.500000" / "argv.txt").read_text()
    assert str(s6 / "checkpoint" / "ok" / "1" / "_1_0.500000_.zstd") in argv and "--no-diff" in argv
    # the baseline: a copy of this run's point dirs, as if made with another checkpoint set
    import shutil
    shutil.copytree(rdir / "run", tmp_path / "refroot")
    res = s.submit("t/ck", ["run", "post"], bin_ref=str(fake_emu[0]))
    pid = res["tasks"][1]["id"]
    run_until(s, lambda: s.tasks[pid].state in ("succeeded", "failed"))
    assert s.tasks[pid].state == "succeeded", s.tasks[pid].jobs[0].failure
    summ = store.read_json(Path(res["runset_dir"]) / "post" / "pk" / "summary.json")
    assert summ["ckpt_mismatch"] is True and summ["ckpt_mismatch_with"] == {"ref": "older_set"}
    assert summ["checkpoint"] == "spec06_fake_set" and summ["ref.ipc_geomean"] == summ["self.ipc_geomean"]
