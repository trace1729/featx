"""Scheduler integration on localhost with a fake emu (real featx-exec, real files)."""

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from featx import recipes, service, store
from featx.events import Inbox
from featx.sched import executor
from featx.sched.scheduler import SchedError, Scheduler
from featx.tree import Tree


class LocalPlacement:
    def refresh(self, force=False):
        pass

    def choose(self, cores, mem_gb, jobs_per_host, avoid=None, pin=None):
        return "localhost"

    def mark_bad(self, host, seconds=300):
        pass

    def snapshot(self, jobs_per_host):
        return []


def make_node(fake_emu, points, steps=None):
    emu, traces = fake_emu
    Tree().add("t/sched/x", parents=True)
    # no emu in the recipe: the default ${bin} must be replaced by submit's bin_ref in the command too
    service.recipe_set("t/sched/x", "run", {"trace_root": str(traces), "points": points,
                                            "W": 1, "I": 300000, "dramsim3_home": None})
    service.recipe_set("t/sched/x", "post", {"steps": steps or {"pk": {"plugin": "perf-keys", "baselines": []}}})


def run_until(s, cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        s.tick()
        if cond():
            return
        time.sleep(0.4)
    raise AssertionError("condition not reached; tick errors: " + "\n".join(s.tick_errors[-3:]))


def sched():
    return Scheduler(Inbox(), placement=LocalPlacement())


def test_run_post_success_seals(root, fake_emu):
    make_node(fake_emu, ["ok_1_0.5"])
    s = sched()
    res = s.submit("t/sched/x", ["run", "post"], bin_ref=str(fake_emu[0]))
    run_id, post_id = (t["id"] for t in res["tasks"])
    run_until(s, lambda: s.tasks[post_id].state in ("succeeded", "failed"))
    assert s.tasks[run_id].state == "succeeded", s.tasks[run_id].failure
    assert s.tasks[post_id].state == "succeeded", s.tasks[post_id].jobs[0].failure
    r = service.result_get(res["runset"])
    assert r["sealed"]
    rdir = Path(res["runset_dir"])
    assert (rdir / "bin" / "emu").resolve() == fake_emu[0].resolve() and r["bin"]["imported"]
    summ = r["post"]["pk"]
    assert summ["self.finished"] == 1 and summ["self.ipc_geomean"] == pytest.approx(0.5)
    types = [e["type"] for e in s.inbox.query(unacked=True)]
    assert "runset_done" in types and "failure" not in types
    # sealed runsets refuse new tasks
    with pytest.raises(SchedError, match="sealed"):
        s.submit("t/sched/x", ["post"], runset=res["runset"])
    # history feeds the ETA of the next submission
    assert s.history.median("run:ok_1_0.5:1:300000") is not None


def test_design_failure_event_and_relaunch_guard(root, fake_emu):
    make_node(fake_emu, ["ok_1_0.5", "abort_1_0.5"])
    s = sched()
    res = s.submit("t/sched/x", ["run", "post"], bin_ref=str(fake_emu[0]))
    run_id, post_id = (t["id"] for t in res["tasks"])
    run_until(s, lambda: s.tasks[run_id].state == "failed")
    fails = s.inbox.query(types=["failure"])
    assert len(fails) == 1 and fails[0]["signature"] == "TraceRTL Error at inst 77"
    assert fails[0]["job"] == "abort_1_0.5" and "TraceRTL Error" in fails[0]["tail"]
    assert s.tasks[run_id].failure["signatures"] == {"TraceRTL Error at inst 77": 1}
    s.tick()
    assert s.tasks[post_id].state == "queued" and "dependency failed" in s.tasks[post_id].note

    with pytest.raises(SchedError, match="problem-analysis"):
        s.relaunch(run_id)
    for i in range(2):   # budget = 2 design relaunches
        service.problem_log_append(res["runset"], problem=f"p{i}", solution="s", task_id=run_id)
        out = s.relaunch(run_id, reason="try")
        assert out["requeued"] == ["abort_1_0.5"]
        run_until(s, lambda: s.tasks[run_id].state == "failed")
        assert s.tasks[run_id].jobs[1].attempt == i + 2
        assert s.tasks[run_id].jobs[0].attempt == 1           # succeeded point untouched
    service.problem_log_append(res["runset"], problem="p", solution="s")
    with pytest.raises(SchedError, match="budget exhausted"):
        s.relaunch(run_id)
    assert s.inbox.query(types=["budget_exhausted"])
    s.unblock(run_id, extra=1)
    assert s.relaunch(run_id)["design_relaunches"] == 3

    # resubmit: new derived runset with only the failed point
    s.cancel(run_id)
    service.problem_log_append(res["runset"], problem="p", solution="s")
    new = s.resubmit(run_id)
    assert new["runset"] != res["runset"]
    rt = s.tasks[new["tasks"][0]["id"]]
    assert rt.spec["points"] == ["abort_1_0.5"]
    assert Tree().find_runset(new["runset"])[1].derived_from == res["runset"]
    s.cancel(rt.id)
    assert all(s.tasks[t["id"]].state == "cancelled" for t in new["tasks"])


def test_heartbeat_death_infra_retry(root, fake_emu):
    make_node(fake_emu, ["hang_1_0.5"])
    s = sched()
    res = s.submit("t/sched/x", ["run"], bin_ref=str(fake_emu[0]))
    tid = res["tasks"][0]["id"]
    rdir = Tree().find_runset(res["runset"])
    jdir = rdir[0].dir / rdir[1].name / "run" / "hang_1_0.5"

    def kill_attempt(att):
        st = executor.read_state(jdir, att)
        os.killpg(int(st["pid"]), signal.SIGKILL)   # host "dies": no exit_code, heartbeat stops
        return int(st["pid"])

    def session_procs(sid):
        out = subprocess.run(["pgrep", "-s", str(sid)], stdout=subprocess.PIPE, text=True).stdout
        return out.split()

    for att in (1, 2):
        run_until(s, lambda: s.tasks[tid].jobs[0].state == "running" and s.tasks[tid].jobs[0].attempt == att)
        assert (jdir / "simulator_out.txt").exists()
        sid = kill_attempt(att)
        # `timeout` moved the emu into its own process group, so killpg left it running
        assert session_procs(sid)
        if att == 1:
            run_until(s, lambda: s.tasks[tid].jobs[0].attempt == 2)
            # infra retry kills the orphan of the dead attempt (whole session, not just the pgid)
            run_until(s, lambda: not session_procs(sid), timeout=10)
            ev = s.inbox.query(types=["retry"])
            assert ev and "heartbeat stale" in ev[0]["signature"]
            # previous attempt's output was archived away from the job dir
            run_until(s, lambda: (jdir / ".fx" / "1" / "simulator_out.txt").exists())
    run_until(s, lambda: s.tasks[tid].state == "failed")
    run_until(s, lambda: not session_procs(sid), timeout=10)   # orphan killed past the budget too
    job = s.tasks[tid].jobs[0]
    assert [h["class"] for h in job.history] == ["infra", "infra"]
    # the failure bundle is on disk for the LLM
    assert (jdir / "failure.json").is_file()
    # infra failure past the retry budget: relaunch does not need a problem-log entry
    assert s.relaunch(tid)["requeued"] == ["hang_1_0.5"]
    run_until(s, lambda: s.tasks[tid].jobs[0].state == "running")
    s.cancel(tid)
    assert s.tasks[tid].state == "cancelled"
    time.sleep(0.5)
    st = executor.read_state(jdir, s.tasks[tid].jobs[0].attempt)
    assert executor.alive_on_host("localhost", st["pid"]) is False
    assert not session_procs(st["pid"])          # the emu went too


def test_scripts_inherited_in_build_run_post(root, fake_emu, tmp_path):
    """${scripts} = the runset's copy of inherited node scripts, in build, run and inline post steps."""
    emu, _ = fake_emu
    make_node(fake_emu, ["ok_1_0.5"], steps={
        "pk": {"plugin": "perf-keys", "baselines": []},      # built-in helpers moved to ${plugin.scripts}
        "st": {"cmd": "bash ${scripts}/stats.sh ${out}", "after": ["pk"]},
    })
    (tmp_path / "wd").mkdir()
    service.recipe_set("t/sched/x", "build", {"type": "script", "workdir": str(tmp_path / "wd"), "cores": 1,
                                              "mem_gb": 1, "script": "bash ${scripts}/build.sh"})
    recipes.patch_own(Tree().get("t/sched/x"), "run", {"args": "$(bash ${scripts}/args.sh)"})
    # parent scripts reach the child; the child's own copy of a name wins
    service.script_write("t/sched", "build.sh", f"mkdir -p build && cp {emu} build/emu\n")
    service.script_write("t/sched", "stats.sh", 'echo \'{"value": 0}\' > "$1/summary.json"\n')
    service.script_write("t/sched/x", "stats.sh", 'echo \'{"value": 1}\' > "$1/summary.json"\n')
    service.script_write("t/sched/x", "args.sh", "echo --fake-flag\n")
    s = sched()
    res = s.submit("t/sched/x", ["build", "run", "post"])
    rdir = Path(res["runset_dir"])
    b_id, r_id, p_id = (t["id"] for t in res["tasks"])
    assert s.tasks[b_id].spec["script"] == f"bash {rdir}/scripts/build.sh"
    assert f"bash {rdir}/scripts/args.sh" in s.tasks[r_id].spec["command"]
    assert sorted(p.name for p in (rdir / "scripts").iterdir()) == ["args.sh", "build.sh", "stats.sh"]
    run_until(s, lambda: s.tasks[p_id].state in ("succeeded", "failed"))
    for t in (b_id, r_id, p_id):
        assert s.tasks[t].state == "succeeded", (s.tasks[t].failure, [j.failure for j in s.tasks[t].jobs])
    post = service.result_get(res["runset"])["post"]
    assert post["st"]["value"] == 1 and post["pk"]["self.finished"] == 1
    assert not s.tasks[b_id].outputs.get("cache_hit")

    # same scripts in another runset -> cache hit; edited build script -> new key
    again = s.submit("t/sched/x", ["build"])
    t2 = s.tasks[again["tasks"][0]["id"]]
    run_until(s, lambda: t2.state in ("succeeded", "failed"))
    assert t2.outputs.get("cache_hit") and t2.outputs["bincache_key"] == s.tasks[b_id].outputs["bincache_key"]
    service.script_write("t/sched", "build.sh", f"# v2\nmkdir -p build && cp {emu} build/emu\n")
    edited = s.submit("t/sched/x", ["build"])
    t3 = s.tasks[edited["tasks"][0]["id"]]
    s.tick()
    assert t3.outputs["bincache_key"] != t2.outputs["bincache_key"] and not t3.outputs.get("cache_hit")
    run_until(s, lambda: t3.state in ("succeeded", "failed"))


def test_relaunch_refreshes_scripts_and_build_key(root, fake_emu, tmp_path):
    emu, _ = fake_emu
    make_node(fake_emu, ["ok_1_0.5"], steps={"st": {"cmd": "bash ${scripts}/stats.sh ${out}"}})
    (tmp_path / "wd").mkdir()
    service.recipe_set("t/sched/x", "build", {"type": "script", "workdir": str(tmp_path / "wd"), "cores": 1,
                                              "mem_gb": 1, "script": "bash ${scripts}/build.sh"})
    service.script_write("t/sched/x", "build.sh", "echo 'foo.cpp:1:1: error: broken' >&2; exit 1\n")
    service.script_write("t/sched/x", "stats.sh", "exit 1\n")
    s = sched()
    res = s.submit("t/sched/x", ["build"])
    b = s.tasks[res["tasks"][0]["id"]]
    run_until(s, lambda: b.state == "failed")
    bad_key = b.outputs["bincache_key"]
    service.script_write("t/sched/x", "build.sh", f"mkdir -p build && cp {emu} build/emu\n")
    service.problem_log_append(res["runset"], problem="build.sh broken", solution="fixed it")
    s.relaunch(b.id)
    run_until(s, lambda: b.state in ("succeeded", "failed"))
    assert b.state == "succeeded", [j.failure for j in b.jobs]
    # the fixed binary is cached under the fixed scripts' key, never under the failed one
    assert b.outputs["bincache_key"] != bad_key
    assert Path(b.outputs["emu"]).parent.name == b.outputs["bincache_key"]

    res = s.submit("t/sched/x", ["post"])
    p = s.tasks[res["tasks"][0]["id"]]
    run_until(s, lambda: p.state == "failed")
    service.script_write("t/sched/x", "stats.sh", 'echo \'{"value": 2}\' > "$1/summary.json"\n')
    service.problem_log_append(res["runset"], problem="stats.sh exit 1", solution="write summary")
    s.relaunch(p.id)
    run_until(s, lambda: p.state in ("succeeded", "failed"))
    assert p.state == "succeeded" and service.result_get(res["runset"])["post"]["st"]["value"] == 2
