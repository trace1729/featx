"""Presets: build tool/mode/options, run mode/workload, and their lowering at submit."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from featx import config, jobs, points, presets, provenance, recipes, service, store
from featx.post import plugins
from featx.sched.scheduler import SchedError
from featx.tree import Tree

from test_sched_local import run_until, sched

# featx/defaults/recipes/build.yaml before presets (master 6599d0f): the default build must
# lower to exactly these args and keep this bincache key, so existing cached builds still hit.
PRE_PRESETS_BUILD = yaml.safe_load(r"""
type: gsim-two-step
cores: 64
mem_gb: 96
host: null
workdir: ${worktree}
timeout_hours: 10
cache: true
artifact: build/emu
env:
  NOOP_HOME: ${worktree}
  NEMU_HOME: ${worktree}/ready-to-run
patches: []
gsim:
  gsim_bin: /nfs/share/bin/gsim
  dramsim3_home: /opt/DRAMsim3
  jobs: 64
  step1_args: EMU_THREADS=1 TRACERTL_MODE=1 WITH_DRAMSIM3=1 WITH_CONSTANTIN=0 WITH_CHISELDB=0 EMU_OPTIMIZE=-O3
  step2_args: SIM_TOP=SimTop NUM_CORES=1 RTL_SUFFIX=sv TRACERTL_MODE=1 WITH_DRAMSIM3=1 EMU_THREADS=1 WITH_CHISELDB=1 WITH_CONSTANTIN=0 EMU_OPTIMIZE=-O3
  reuse_build_from: null
script: null
""")


def _build(overrides=None):
    tree = Tree()
    if "t/b" not in [e.rel for e in tree.by_id.values()]:
        tree.add("t/b", parents=True)
        tree = Tree()
    # ${paths.*} substituted as at submit (${worktree} left as is: the key below has it literally)
    spec = recipes.substitute(recipes.effective(tree, tree.get("t/b"), "build", overrides), recipes.path_vars())
    return presets.lower_build(spec)


def test_default_build_lowers_to_pre_presets_args_and_key(migrated):
    spec = _build()
    assert spec["tool"] == "gsim" and spec["mode"] == "trace" and spec["type"] == "gsim-two-step"
    assert spec["gsim"]["step1_args"] == PRE_PRESETS_BUILD["gsim"]["step1_args"]
    assert spec["gsim"]["step2_args"] == PRE_PRESETS_BUILD["gsim"]["step2_args"]
    # (the old effective recipe went through the same merge, which drops `key: null`)
    old = config.deep_merge({}, PRE_PRESETS_BUILD)
    assert provenance.bincache_key({}, presets.key_recipe(spec)) == provenance.bincache_key({}, old)


def test_build_options_mode_and_raw_args(root):
    spec = _build({"mode": "exec", "options": {"chiseldb": True, "chiselmap": True, "threads": 8,
                                                "flags": {"DISABLE_PERF": 1}, "extra": "FOO=bar"},
                   "gsim": {"step1_args": "WITH_CHISELDB=0"}})
    s1, s2 = spec["gsim"]["step1_args"], spec["gsim"]["step2_args"]
    assert "TRACERTL_MODE=0" in s1 and "EMU_THREADS=8" in s1 and "WITH_CHISELDB=1" in s1
    assert s1.endswith("WITH_CHISELMAP=1 DISABLE_PERF=1 FOO=bar WITH_CHISELDB=0")   # raw args last: they win
    assert "TRACERTL_MODE=0" in s2 and "WITH_CHISELMAP=1" in s2
    assert "WITH_CHISELMAP" not in _build()["gsim"]["step1_args"]
    # a different mode is a different binary
    assert presets.key_recipe(spec) != presets.key_recipe(_build())


def test_verilator_build_script(migrated):
    spec = _build({"tool": "verilator", "verilator": {"args": "EXTRA=1"}})
    assert spec["type"] == "verilator"
    assert spec["verilator"]["make_args"].startswith("NUM_CORES=1 RTL_SUFFIX=sv TRACERTL_MODE=1")
    assert spec["verilator"]["make_args"].endswith("EXTRA=1")
    spec["workdir"] = "/wd"
    script = jobs.build_script("T", spec)
    assert "export PATH=/opt/verilator/bin:$PATH" in script
    assert "make emu -j64 DRAMSIM3_HOME=" in script and "make gsim" not in script
    assert "gsim" not in presets.key_recipe(spec) and "verilator" in presets.key_recipe(spec)


def test_legacy_type_and_bad_tool(root):
    Tree().add("t/b", parents=True)
    service.recipe_set("t/b", "build", {"type": "script", "script": "true"})
    assert _build()["tool"] == "script" and _build()["type"] == "script"
    # a later layer's tool wins over an inherited legacy type
    assert _build({"tool": "gsim"})["type"] == "gsim-two-step"
    with pytest.raises(presets.PresetError, match="build.tool"):
        _build({"tool": "vcs"})


def test_run_mode_follows_build(root):
    assert presets.run_mode({"mode": None}, None) == "trace"
    assert presets.run_mode({"mode": None}, {"mode": "exec"}) == "exec"
    with pytest.raises(presets.PresetError, match="build is mode=trace"):
        presets.run_mode({"mode": "exec"}, {"mode": "trace"})
    assert presets.lower_run({"mode": None, "commands": {"trace": "x"}}, {"options": {"threads": 4}})["cores_per_point"] == 4


def _run_spec(**kw):
    spec = store.read_yaml(config.DEFAULTS_DIR / "recipes" / "run.yaml")
    spec.update(kw)
    return presets.lower_run(spec, None)


def test_exec_point_command(root):
    ctx = {"point.name": "b_1_0.5", "point.bench": "b", "point.cp": "1", "point.weight": "0.5",
           "emu": "/emu", "W": 1, "I": 2, "args": ""}
    spec = _run_spec(mode="exec", checkpoint_file="/ck/${point.bench}/_${point.cp}_.gz", dump_db=True,
                     exec={"diff": "/nemu.so", "gcpt_restorer": "/gcpt.bin"})
    cmd, inputs = presets.point_command(spec, ctx)
    assert cmd.startswith("/emu -i /ck/b/_1_.gz --diff /nemu.so -r /gcpt.bin -W 1 -I 2")
    assert '--dump-db --db-path="$PWD/chiseldb.sqlite"' in cmd and "--tracertl-file" not in cmd
    assert inputs == {"checkpoint": "/ck/b/_1_.gz", "diff reference": "/nemu.so"}
    spec = _run_spec(mode="exec", checkpoint_file="/ck/${point.cp}.zstd", exec={"diff": None, "gcpt_restorer": "/g"})
    cmd, inputs = presets.point_command(spec, ctx)
    assert "--no-diff" in cmd and " -r " not in cmd and inputs == {"checkpoint": "/ck/1.zstd"}
    with pytest.raises(presets.PresetError, match="no checkpoint"):
        presets.point_command(_run_spec(mode="exec", checkpoint_file="${point.checkpoint}"), ctx)
    cmd, inputs = presets.point_command(_run_spec(), {**ctx, "trace_root": "/tr"})
    assert "--tracertl-file /tr/b/1/_1_0.5_.trace.zstd" in cmd and "--dump-db" not in cmd
    assert inputs == {"trace file": "/tr/b/1/_1_0.5_.trace.zstd"}


@pytest.fixture
def toy_workload(root, tmp_path):
    tsv = tmp_path / "manifest.tsv"
    rows = [("toy", "ok", "1", "0.5"), ("toy", "ok", "2", "0.25"), ("toy", "abort", "1", "0.5")]
    tsv.write_text("suite\tbenchmark\tpoint\tweight\tcheckpoint\ttrace\tmax_instr\n" + "".join(
        f"{s}\t{b}\t{p}\t{w}\t/ck/{b}/{p}.gz\t/tr/{b}/{p}\t0\n" for s, b, p, w in rows))
    data = yaml.safe_load((root / "featx.yaml").read_text())
    data["workloads"] = config.deep_merge(data.get("workloads") or {}, {
        "toy": {"manifest": str(tsv), "label": "toy-label", "reference": None,
                "run": {"trace_root": "/toy-traces", "checkpoint_file": "${point.checkpoint}"}}})
    (root / "featx.yaml").write_text(yaml.safe_dump(data))
    config.reload_settings()
    Tree().add("t/w", parents=True)
    return tsv


def test_workload_layer_and_points(toy_workload):
    tree = Tree()
    service.recipe_set("t/w", "run", {"workload": "toy"})
    spec = recipes.effective(tree, tree.get("t/w"), "run")
    assert spec["trace_root"] == "/toy-traces"
    assert points.resolve(spec) == ["ok_1_0.5", "ok_2_0.25", "abort_1_0.5"]
    assert points.resolve({**spec, "benchmarks": ["ok"]}) == ["ok_1_0.5", "ok_2_0.25"]
    assert points.resolve({**spec, "points": ["abort_1_0.5"]}) == ["abort_1_0.5"]
    with pytest.raises(presets.PresetError, match="not in workload toy"):
        points.resolve({**spec, "points": ["nope_1_0.1"]})
    assert presets.point_vars(spec, "ok_2_0.25") == {"point.trace": "/tr/ok/2", "point.checkpoint": "/ck/ok/2.gz"}
    # any recipe layer wins over the workload's keys
    service.recipe_set("t/w", "run", {"workload": "toy", "trace_root": "/mine"})
    assert recipes.effective(Tree(), Tree().get("t/w"), "run")["trace_root"] == "/mine"
    with pytest.raises(presets.PresetError, match="unknown workload"):
        presets.workload("nope")


def test_points_strict_benchmarks_filter(toy_workload):
    tree = Tree()
    service.recipe_set("t/w", "run", {"workload": "toy"})
    spec = recipes.effective(tree, tree.get("t/w"), "run")
    # an inherited benchmarks filter silently dropped explicit points; strict (submit) refuses
    assert points.resolve({**spec, "points": ["abort_1_0.5"], "benchmarks": ["ok"]}) == []
    with pytest.raises(presets.PresetError, match="drops 1 point"):
        points.resolve({**spec, "points": ["abort_1_0.5", "ok_1_0.5"], "benchmarks": ["ok"]}, strict=True)
    with pytest.raises(presets.PresetError, match="matches none of the 3 selected points"):
        points.resolve({**spec, "benchmarks": ["505.mcf_r"]}, strict=True)
    assert points.resolve({**spec, "benchmarks": ["ok"]}, strict=True) == ["ok_1_0.5", "ok_2_0.25"]
    assert points.resolve({**spec, "points": ["abort_1_0.5"], "benchmarks": None}, strict=True) == ["abort_1_0.5"]
    service.recipe_set("t/w", "run", {"workload": "toy", "benchmarks": ["ok"], "points": ["abort_1_0.5"]})
    with pytest.raises(SchedError, match="drops 1 point"):
        sched().submit("t/w", ["run"], bin_ref="/bin/true")
    assert Tree().runsets(Tree().get("t/w")) == []


def test_failed_submit_leaves_no_runset(toy_workload):
    service.recipe_set("t/w", "run", {"workload": "toy", "points": ["nope_1_0.1"]})
    s = sched()
    with pytest.raises(SchedError, match="not in workload"):
        s.submit("t/w", ["run"], bin_ref="/bin/true")
    assert Tree().runsets(Tree().get("t/w")) == []


def test_post_workload_label_and_missing_reference(nfs_site, toy_workload, tmp_path):
    tree = Tree()
    entry = tree.get("t/w")
    rdir = entry.dir / "20261013-1"
    store.write_yaml(rdir / "experiment.yaml", {"recipes": {"run": {"workload": "toy"}}})
    task = SimpleNamespace(node_id=entry.node.id, runset_name=rdir.name, spec={"steps": {"collect": {}, "pair": {}}})
    cmd, _ = plugins.render(tree, task, "collect", {}, rdir, rdir / "post" / "collect")
    assert "--suite toy-label" in cmd
    with pytest.raises(ValueError, match="no reference baseline"):
        plugins.render(tree, task, "pair", {}, rdir, rdir / "post" / "pair")
    cmd, _ = plugins.render(tree, task, "pair", {"with": {"reference": "/some/ref"}}, rdir, rdir / "post" / "pair")
    assert "ensure_collected.py /some/ref" in cmd
    # no workload: the site's default_workload (spec06: the exec-reference baseline), as before presets
    store.write_yaml(rdir / "experiment.yaml", {"recipes": {"run": {}}})
    cmd, _ = plugins.render(tree, task, "pair", {}, rdir, rdir / "post" / "pair")
    assert "--suite spec06" in cmd and "/baselines/spec06/exec" in cmd
    # spec-score: no baselines by default, the spec06 insts json, and the workload label
    cmd, _ = plugins.render(tree, task, "spec-score", {}, rdir, rdir / "post" / "spec-score")
    assert f"--runs self={rdir / 'run'}  --points" in cmd     # empty baselines
    assert "spec06_gcc15_rv64gcb_base_260122/checkpoint-0-0-0/cluster-0-0.json" in cmd
    assert '--label "spec06"' in cmd and "--version auto" in cmd
    store.write_yaml(rdir / "experiment.yaml", {"recipes": {"run": {"workload": "spec17"}}})
    cmd, _ = plugins.render(tree, task, "spec-score", {"baselines": ["/b/root"]}, rdir, rdir / "post" / "spec-score")
    assert "featx/defaults/insts/spec17_rv64gcb_o2_20m.json" in cmd and "b:root=/b/root" in cmd


def test_exec_run_with_fake_emu(root, fake_emu):
    emu, traces = fake_emu
    Tree().add("t/x", parents=True)
    service.recipe_set("t/x", "run", {
        "mode": "exec", "points": ["ok_1_0.5"], "W": 1, "I": 300000, "dramsim3_home": None,
        "checkpoint_file": f"{traces}/${{point.bench}}/${{point.cp}}/_${{point.cp}}_${{point.weight}}_.trace.zstd",
        "exec": {"diff": None}})
    s = sched()
    res = s.submit("t/x", ["run"], bin_ref=str(emu))
    rid = res["tasks"][0]["id"]
    run_until(s, lambda: s.tasks[rid].state in ("succeeded", "failed"))
    assert s.tasks[rid].state == "succeeded", [j.failure for j in s.tasks[rid].jobs]
    argv = (Path(res["runset_dir"]) / "run" / "ok_1_0.5" / "argv.txt").read_text()
    assert argv.startswith(f"-i {traces}/ok/1/") and "--no-diff" in argv and "--tracertl-file" not in argv
    assert s.tasks[rid].spec["mode"] == "exec"


def test_points_coverage():
    names = ["a_1_0.5", "a_2_0.3", "a_3_0.2", "b_9_0.3", "b_4_0.3", "b_5_0.4"]
    # a: 0.5 alone reaches 0.5; b: 0.4, then the 0.3 tie goes to the lower checkpoint (4)
    assert points.by_coverage(names, 0.5) == ["a_1_0.5", "b_4_0.3", "b_5_0.4"]
    # 0.8: a needs 0.5+0.3; b's 0.4+0.3 = 0.7 is still short, so all three
    assert points.by_coverage(names, 0.8) == ["a_1_0.5", "a_2_0.3", "b_9_0.3", "b_4_0.3", "b_5_0.4"]
    assert points.by_coverage(names, 1.0) == names
    for bad in (0, 1.5, "x"):
        with pytest.raises(presets.PresetError):
            points.resolve({"points": names, "coverage": bad})
    with pytest.raises(presets.PresetError, match="coverage"):
        points.resolve({"points": names, "coverage": 0.5}, strict=True)


def test_points_coverage_on_workload(toy_workload):
    every = points.resolve({"workload": "toy"})
    picked = points.resolve({"workload": "toy", "coverage": 0.3})
    assert picked and set(picked) <= set(every)
    assert picked == points.by_coverage(every, 0.3)
