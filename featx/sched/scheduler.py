"""The scheduler: single writer of tasks/*.json and the event inbox.

Task lifecycle: queued (waiting for deps / jobs not yet materialized) -> running (jobs
materialized) -> succeeded | failed | cancelled. Jobs: queued -> launching -> running ->
succeeded | failed | cancelled. Infra failures retry automatically on another host; design
failures emit an event and wait for a relaunch (guarded by problem-analysis.log).
"""

from __future__ import annotations

import math
import os
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import config, jobs as jobgen, points as ptmod, presets, problemlog, provenance, recipes, store, taskstore
from ..model import ACTIVE_TASK_STATES, JOB_ACTIVE, TERMINAL_TASK_STATES, Job, Task
from ..tree import Tree, TreeError
from . import classify, eta, executor
from .placement import Placement


PROGRESS_EVERY = 30.0   # seconds between progress-log parses / bookkeeping saves per task


class SchedError(Exception):
    pass


def launch_order(task: Task) -> tuple:
    """Higher priority first, then oldest first."""
    return (-task.priority, task.created)


def _now() -> float:
    return time.time()


class Scheduler:
    def __init__(self, inbox, *, placement: Placement | None = None) -> None:
        self.lock = threading.RLock()
        self.inbox = inbox
        self.placement = placement or Placement()
        self.history = eta.History()
        self.tasks: dict[str, Task] = taskstore.load_all()
        self.tick_errors: list[str] = []
        self.last_tick = 0.0
        self._progress_at: dict[tuple[str, str], float] = {}
        self._saved_at: dict[str, float] = {}
        # (host, job dir, attempt, deadline): launches that completed after their job was cancelled
        self._orphans: list[tuple[str, Path, int, float]] = []
        # ssh work is queued under the lock and done without it (a 125-point cancel or a host
        # full of stale heartbeats meant minutes of serial sshs that froze the API and the UI)
        self._kills: list[tuple[str, int, str]] = []                     # (host, pid, signal)
        self._suspects: dict[tuple[str, str, int], tuple[str, int]] = {}  # job key -> (host, pid)
        self._alive: dict[tuple[str, str, int], bool | None] = {}         # job key -> probe result
        self._ssh_lock = threading.Lock()
        self._readopt()

    def _readopt(self) -> None:
        """Daemon (re)start: jobs kept running without us; restart the liveness clocks so
        the downtime is not mistaken for stale heartbeats."""
        now = _now()
        for task in self.tasks.values():
            for job in task.jobs:
                if job.state == "running":
                    job.hb_seen = now
                elif job.state == "launching":
                    job.launched = now

    # =============================================================== helpers
    def _save(self, task: Task) -> None:
        taskstore.save(task)
        self._saved_at[task.id] = _now()

    def _tree(self) -> Tree:
        return Tree()

    def _runset_dir(self, tree: Tree, task: Task) -> Path:
        entry = tree.get(task.node_id)
        return entry.dir / task.runset_name

    def _job_dir(self, tree: Tree, task: Task, job: Job) -> Path:
        return self._runset_dir(tree, task) / job.rel

    def cores_in_use(self, kind: str | None = None) -> int:
        """Cores reserved by featx's launching/running remote jobs (local post steps excluded)."""
        return sum(j.cores for t in self.tasks.values() if kind is None or t.kind == kind for j in t.jobs
                   if j.state in ("launching", "running") and not j.local)

    def max_total_cores(self) -> int:
        return int(config.settings()["placement"].get("max_total_cores") or 0)

    def max_build_cores(self) -> int:
        return int(config.settings()["placement"].get("max_build_cores") or 0)

    def _queue_kill(self, host: str, pid: str | int, signal: str = "TERM") -> None:
        self._kills.append((host, int(pid), signal))

    def _do_ssh_work(self, blocking: bool = True) -> None:
        """Run the queued kills and liveness probes, one ssh per host, hosts in parallel.
        Called without self.lock; _ssh_lock keeps two callers from sending the same kills.
        Non-blocking callers leave the work to the next tick when another caller is busy."""
        if not self._ssh_lock.acquire(blocking=blocking):
            return
        try:
            with self.lock:
                kills, self._kills = self._kills, []
                suspects, self._suspects = self._suspects, {}
            if not kills and not suspects:
                return
            work: dict[tuple[str, str], list[int]] = {}
            for host, pid, sig in kills:
                work.setdefault(("kill:" + sig, host), []).append(pid)
            for host, pid in suspects.values():
                work.setdefault(("alive", host), []).append(pid)

            def one(item):
                (what, host), pids = item
                if what == "alive":
                    return what, executor.alive_many(host, pids)
                return what, executor.kill_many(host, pids, what.split(":", 1)[1])

            with ThreadPoolExecutor(max_workers=16) as pool:
                results = list(pool.map(one, work.items()))
            alive: dict[tuple[str, int], bool | None] = {}
            for ((what, host), _), (_, res) in zip(work.items(), results):
                if what == "alive":
                    alive.update({(host, pid): v for pid, v in res.items()})
            with self.lock:
                for key, (host, pid) in suspects.items():
                    self._alive[key] = alive.get((host, pid))
        finally:
            self._ssh_lock.release()

    def jobs_per_host(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.tasks.values():
            for j in t.jobs:
                if j.state in ("launching", "running") and j.host:
                    out[j.host] = out.get(j.host, 0) + 1
        return out

    # ============================================================ submission
    def submit(self, node_ref: str, kinds: list[str], *, runset: str | None = None,
               purpose: str = "", overrides: dict | None = None, bin_ref: str | None = None,
               after: list[str] | None = None, derived_from: str | None = None,
               only_points: list[str] | None = None, post_steps: list[str] | None = None,
               priority: int = 0) -> dict:
        """Create tasks for a node. kinds ⊆ [build, run, post] in pipeline order; a new runset
        is created unless `runset` names an existing unsealed one. Higher `priority` tasks get
        freed cores and launch slots first."""
        overrides = overrides or {}
        kinds = [k for k in ("build", "run", "post") if k in kinds]
        if not kinds:
            raise SchedError("nothing to submit")
        with self.lock:
            tree = self._tree()
            entry = tree.get(node_ref)
            if runset:
                _, rs = tree.find_runset(runset if "/" in runset else f"{entry.rel}/{runset}")
                if rs.sealed:
                    raise SchedError(f"runset {rs.name} is sealed (immutable); submit without --runset "
                                     f"to create a new one (use derived_from to link)")
                rdir = entry.dir / rs.name
                created_rs = False
            else:
                rs, rdir = tree.new_runset(entry, purpose=purpose, derived_from=derived_from)
                created_rs = True
            try:
                created = self._submit_tasks(tree, entry, rs, rdir, kinds, overrides, bin_ref, after,
                                             only_points, post_steps)
            except Exception:
                # A submit that fails validation must not leave an empty runset behind.
                if created_rs:
                    shutil.rmtree(rdir, ignore_errors=True)
                raise
            emu_override = next((t.spec["emu"] for t in created if t.kind == "run"), None) if bin_ref else None
            for task in created:
                task.priority = int(priority or 0)
                rs.recipes[task.kind] = task.spec
                rs.tasks.append(task.id)
            if emu_override and not rs.bin:
                # Same layout as a build: ${bin} = <runset>/bin/emu and experiment.yaml records it.
                (rdir / "bin").mkdir(parents=True, exist_ok=True)
                link = rdir / "bin" / "emu"
                if not link.exists():
                    os.symlink(emu_override, link)
                manifest = store.read_json(Path(emu_override).parent / "manifest.json", {})
                rs.bin = {"key": manifest.get("key") or (bin_ref if provenance.bincache_lookup(bin_ref) else None),
                          "emu": emu_override, "sha256": manifest.get("sha256"), "cached": True, "imported": True}
            if entry.node.git and not rs.provenance:
                rs.provenance = provenance.worktree_state(entry.node.git.worktree)
                saved = provenance.save_patches(entry.node.git.worktree, rdir / "src-patches")
                if saved:
                    rs.provenance["saved_patches"] = saved
            tree.save_runset(entry, rs)
            for task in created:
                self.tasks[task.id] = task
                self._save(task)
            self.inbox.emit("submitted", node=entry.rel, runset=rs.name,
                            tasks=[{"id": t.id, "kind": t.kind} for t in created])
            return {"runset": f"{entry.rel}/{rs.name}", "runset_dir": str(rdir),
                    "tasks": [{"id": t.id, "kind": t.kind, "after": t.after} for t in created]}

    def _submit_tasks(self, tree: Tree, entry, rs, rdir: Path, kinds: list[str], overrides: dict,
                      bin_ref: str | None, after: list[str] | None, only_points: list[str] | None,
                      post_steps: list[str] | None) -> list[Task]:
        """Effective, substituted and lowered (presets) specs of the new tasks, validated."""
        recipes.snapshot_scripts(tree, entry, rdir)
        ctx = recipes.base_context(tree, entry, rdir, rs.name)
        emu_override = bin_mode = None
        if bin_ref and "run" in kinds:
            # Resolve before substitution: `command` embeds ${emu}, so a late spec["emu"]
            # assignment would leave the command pointing at the recipe's emu.
            emu_override = self._resolve_bin(tree, bin_ref)
            bin_mode = self._bin_mode(tree, bin_ref, emu_override)
        created: list[Task] = []
        prev: Task | None = None
        for kind in kinds:
            ov = overrides.get(kind)
            if kind == "run" and emu_override:
                ov = {**(ov or {}), "emu": emu_override}
            try:
                spec = recipes.effective(tree, entry, kind, ov)
                built = prev.spec if prev is not None and prev.kind == "build" else \
                    ({"mode": bin_mode} if bin_mode else None)
                if kind == "run" and spec.get("checkpoint") and presets.run_mode(spec, built) == "exec":
                    # the set's images, before the commands embed a workload's checkpoint_file template
                    spec["checkpoint_file"] = "${point.checkpoint}"
                spec = recipes.substitute(spec, {**ctx, **recipes.scalar_vars(spec)})
                if kind == "build":
                    spec = presets.lower_build(spec)
                if kind == "run":
                    if only_points:
                        spec["points"], spec["points_file"], spec["suite"] = list(only_points), None, None
                    spec = presets.lower_run(spec, built)
            except presets.PresetError as error:
                raise SchedError(str(error)) from None
            if kind == "post" and post_steps:
                spec["steps"] = {k: v for k, v in (spec.get("steps") or {}).items() if k in post_steps}
            deps = list(after or []) if prev is None else [prev.id]
            task = Task(id=store.new_id(), kind=kind, name=f"{kind}", node_id=entry.node.id,
                        runset_id=rs.id, runset_name=rs.name, after=deps, spec=spec,
                        created=store.now_iso(),
                        max_design_relaunches=config.settings()["retry"]["max_design_relaunches"])
            try:
                self._validate(tree, entry, task, prev)
            except presets.PresetError as error:
                raise SchedError(str(error)) from None
            created.append(task)
            prev = task
        return created

    @staticmethod
    def _bin_mode(tree: Tree, ref: str, emu: str) -> str | None:
        """trace|exec of a --bin binary: its runset's build recipe, else its bincache manifest."""
        try:
            _, rs = tree.find_runset(ref)
            if rs.recipes.get("build"):
                return presets.build_mode(rs.recipes["build"])
        except (TreeError, presets.PresetError):
            pass
        return store.read_json(Path(emu).parent / "manifest.json", {}).get("mode")

    def _resolve_bin(self, tree: Tree, ref: str) -> str:
        p = Path(ref)
        if p.is_file():
            return str(p.resolve())
        hit = provenance.bincache_lookup(ref)
        if hit:
            return str(hit)
        try:
            entry, rs = tree.find_runset(ref)
        except TreeError as error:
            raise SchedError(f"--bin {ref}: not a file, bincache key or runset ({error})")
        if not rs.bin:
            raise SchedError(f"runset {ref} has no built binary")
        return rs.bin["emu"]

    def _validate(self, tree: Tree, entry, task: Task, prev: Task | None) -> None:
        spec = task.spec
        if task.kind == "build":
            if spec.get("type") not in ("gsim-two-step", "verilator", "script"):
                raise SchedError(f"build.tool must be gsim|verilator|script, got {spec.get('tool', spec.get('type'))}")
            if not Path(spec["workdir"]).is_dir():
                raise SchedError(f"build workdir {spec['workdir']} missing (create the node worktree first: "
                                 f"featx worktree create {entry.rel})")
            if spec.get("type") == "script" and not spec.get("script"):
                raise SchedError("build.type=script needs build.script")
            busy = [t for t in self.tasks.values() if t.kind == "build" and t.state in ACTIVE_TASK_STATES
                    and t.spec.get("workdir") == spec["workdir"]]
            if busy:
                raise SchedError(f"another build is active in {spec['workdir']}: {busy[0].id}")
        elif task.kind == "run":
            if spec.get("executor") not in ("points", "perf_trigger"):
                raise SchedError("run.executor must be points|perf_trigger")
            if spec.get("executor") == "points" and not ptmod.resolve(spec, strict=True):
                raise SchedError("run recipe selects no points (set points / points_file / suite / workload)")
            if spec.get("executor") == "perf_trigger" and not spec["perf_trigger"].get("json_path"):
                raise SchedError("perf_trigger executor needs perf_trigger.json_path")
            if (spec.get("executor") == "points" and spec.get("mode") == "trace" and not spec.get("command")
                    and "${trace_root}" in str(spec.get("trace_file") or "") and not spec.get("trace_root")):
                raise SchedError("trace mode: run.trace_root is unset (select a run.workload, or set trace_root / trace_file)")
            if spec.get("executor") == "perf_trigger" and not spec["perf_trigger"].get("main"):
                raise SchedError("perf_trigger executor needs perf_trigger.main (paths.perf_trigger in featx.yaml)")
            emu = str(spec.get("emu", ""))
            from_build = prev is not None and prev.kind == "build"
            if not from_build and "${bin}" not in emu and not Path(emu).is_file():
                if emu.endswith("/bin/emu") and Path(emu).parent.parent.is_dir():
                    raise SchedError("run needs a binary: submit with build, or pass --bin <file|cache key|runset>")
                raise SchedError(f"emu not found: {emu}")
        elif task.kind == "post":
            from ..post import plugins
            steps = spec.get("steps") or {}
            if not steps:
                raise SchedError("post recipe has no steps")
            for name, st in steps.items():
                why = plugins.requirement_error(name, plugins.manifest(name, st))
                if why:
                    raise SchedError(why)

    # ========================================================== materialize
    def _deps_state(self, task: Task) -> str:
        states = [self.tasks[d].state if d in self.tasks else "missing" for d in task.after]
        if any(s in ("failed", "cancelled", "missing") for s in states):
            return "broken"
        if all(s == "succeeded" for s in states):
            return "ok"
        return "wait"

    def _dep_of_kind(self, task: Task, kind: str) -> Task | None:
        seen, stack = set(), list(task.after)
        while stack:
            tid = stack.pop()
            if tid in seen or tid not in self.tasks:
                continue
            seen.add(tid)
            t = self.tasks[tid]
            if t.kind == kind:
                return t
            stack.extend(t.after)
        return None

    def _materialize(self, tree: Tree, task: Task) -> None:
        rdir = self._runset_dir(tree, task)
        spec = task.spec
        if task.kind == "build":
            entry = tree.get(task.node_id)
            key = task.outputs["bincache_key"] = self._build_key(task, rdir)
            hit = provenance.bincache_lookup(key) if spec.get("cache", True) else None
            if hit:
                task.outputs["emu"] = str(hit)
                task.outputs["cache_hit"] = True
                self._finish_build(tree, task, hit, cached=True)
                task.state, task.finished = "succeeded", _now()
                self.inbox.emit("task_done", task=task.id, kind="build", node=entry.rel,
                                runset=task.runset_name, state="succeeded", note="bincache hit")
                self._maybe_seal(tree, task)
                return
            task.jobs = [Job(name="build", rel="build", cores=int(spec.get("cores", 64)),
                             mem_gb=float(spec.get("mem_gb", 96)))]
        elif task.kind == "run":
            if spec.get("emu", "").endswith("${bin}") or "${bin}" in str(spec.get("emu")):
                spec["emu"] = str(rdir / "bin" / "emu")
            b = self._dep_of_kind(task, "build")
            if b and b.outputs.get("emu"):
                spec["emu"] = str(rdir / "bin" / "emu") if (rdir / "bin" / "emu").exists() else b.outputs["emu"]
            if spec["executor"] == "perf_trigger":
                task.jobs = [Job(name="perf_trigger", rel="run/.perf_trigger", cores=1, mem_gb=2, local=True)]
            else:
                names = ptmod.resolve(spec)
                ck = presets.checkpoint_index(spec)
                ptmod.write_config(names, rdir / "run" / "points.json", ck["insts"] if ck else None)
                task.jobs = [Job(name=n, rel=f"run/{n}", cores=int(spec.get("cores_per_point", 1)),
                                 mem_gb=float(spec.get("mem_gb_per_point", 8))) for n in names]
        elif task.kind == "post":
            from ..post import plugins
            steps = spec.get("steps") or {}
            task.jobs = []
            for name, st in steps.items():
                plugins.manifest(name, st)
                task.jobs.append(Job(name=name, rel=f"post/{name}", cores=1, mem_gb=4, local=True,
                                     vars={"after": plugins.step_after(steps, name)}))
        task.state, task.started = "running", _now()

    @staticmethod
    def _build_key(task: Task, rdir: Path) -> str:
        spec = task.spec
        state = provenance.worktree_state(spec["workdir"]) if (Path(spec["workdir"]) / ".git").exists() else {}
        return provenance.bincache_key(state, presets.key_recipe(spec), rdir / "scripts")

    def _finish_build(self, tree: Tree, task: Task, emu: Path, cached: bool) -> None:
        entry = tree.get(task.node_id)
        rdir = entry.dir / task.runset_name
        (rdir / "bin").mkdir(parents=True, exist_ok=True)
        link = rdir / "bin" / "emu"
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(emu, link)
        _, rs = tree.find_runset(f"{entry.rel}/{task.runset_name}")
        manifest = store.read_json(Path(emu).parent / "manifest.json", {})
        rs.bin = {"key": task.outputs.get("bincache_key"), "emu": str(emu), "sha256": manifest.get("sha256"),
                  "cached": cached}
        tree.save_runset(entry, rs)

    # ================================================================ tick
    def tick(self) -> None:
        with self.lock:
            need_launch = self._tick_locked()
        self._do_ssh_work()
        if need_launch:
            self._launch_queued()

    def _tick_locked(self) -> bool:
        """Poll/materialize under the lock; True if some job waits for a launch."""
        self.last_tick = _now()
        self._reap_orphans()
        active = [t for t in self.tasks.values() if t.state in ACTIVE_TASK_STATES and not t.blocked]
        if not active:
            return False
        tree = self._tree()
        need_launch = False
        for task in sorted(active, key=launch_order):
            try:
                if task.state == "queued":
                    dep = self._deps_state(task)
                    note = {"wait": "waiting for dependencies", "broken": "blocked: a dependency failed",
                            "ok": ""}[dep]
                    if task.note != note:
                        task.note = note
                        self._save(task)
                    if dep != "ok":
                        continue
                    self._materialize(tree, task)
                    self._save(task)
                if task.state == "running":
                    self._poll(tree, task)
                    need_launch |= any(j.state == "queued" for j in task.jobs)
            except Exception as error:  # noqa: BLE001 — one bad task must not stop the loop
                msg = f"{task.id}: {error!r}"
                self.tick_errors = (self.tick_errors + [msg + "\n" + traceback.format_exc()])[-20:]
                if task.state == "queued":
                    task.state, task.finished = "failed", _now()
                    task.failure = {"class": "design", "signature": f"materialize: {error}"}
                    self._save(task)
                    self.inbox.emit("failure", task=task.id, kind=task.kind, node=self._rel(tree, task),
                                    runset=task.runset_name, signature=task.failure["signature"])
        return need_launch

    def _rel(self, tree: Tree, task: Task) -> str:
        try:
            return tree.get(task.node_id).rel
        except TreeError:
            return task.node_id

    # --------------------------------------------------------------- launch
    def _launch_queued(self) -> None:
        """Reserve + prepare jobs under the lock, ssh-launch them without it, apply the results
        under it again. A big panel's launches take a minute (~1 s per ssh, 16 at a time);
        holding the lock that long blocked submit/cancel/every API call past the client timeout."""
        self.placement.refresh()   # host probes: ssh too, no shared state to guard
        with self.lock:
            todo = self._reserve_launches(self._tree())
        if not todo:
            return
        env = {"FX_HB_INTERVAL": config.settings()["liveness"]["heartbeat_interval"]}
        # ssh launches in parallel (a 200-point panel would take minutes serially)
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda x: executor.launch(x[2], x[3], x[4], env=env), todo))
        with self.lock:
            tree = self._tree()
            now = _now()
            grace = float(config.settings()["liveness"]["launch_grace"])
            for (task, job, host, jdir, attempt), (ok, err) in zip(todo, results):
                if job.state != "launching" or job.attempt != attempt:
                    # Cancelled (maybe already relaunched) while the ssh was in flight: the job
                    # had no pid to kill then, so kill the started attempt once it writes one.
                    if ok:
                        self._orphans.append((host, jdir, attempt, now + grace))
                    continue
                job.host, job.launched = host, now
                if not ok:
                    self._job_failed(tree, task, job, None, err)
            for task in {id(t): t for t, *_ in todo}.values():
                self._refresh_task_state(tree, task)
                self._save(task)

    def _reap_orphans(self) -> None:
        keep = []
        for host, jdir, attempt, deadline in self._orphans:
            st = executor.read_state(jdir, attempt)
            if st.get("pid") and "exit_code" not in st:
                self._queue_kill(host, st["pid"])
            elif "exit_code" not in st and _now() < deadline:
                keep.append((host, jdir, attempt, deadline))
        self._orphans = keep

    def _reserve_launches(self, tree: Tree) -> list[tuple[Task, Job, str, Path, int]]:
        """Pick hosts for queued jobs (at most placement.max_launches_per_tick) and write their
        cmd.sh. The jobs are left 'launching' with no `launched` time until the ssh returns."""
        per_host = self.jobs_per_host()
        cap = int(config.settings()["placement"].get("max_launches_per_tick") or 0)
        core_caps = {"run": self.max_total_cores(), "build": self.max_build_cores()}
        cores_used = {kind: self.cores_in_use(kind) for kind in core_caps}
        # Priority, then FIFO, applies within each budget; queued runs must not block builds.
        core_full = {kind: False for kind in core_caps}
        starts: list[tuple[Task, Job, str]] = []
        for task in sorted((t for t in self.tasks.values() if t.state == "running"), key=launch_order):
            pool = "build" if task.kind == "build" else "run"
            core_cap = core_caps[pool]
            limit = task.spec.get("max_parallel")
            running = sum(1 for j in task.jobs if j.state in ("launching", "running"))
            changed = False
            waiting = False
            for job in task.jobs:
                if job.state != "queued":
                    continue
                if limit and running >= int(limit):
                    break
                if cap and len(starts) >= cap:
                    break   # the rest go next tick
                # (a job bigger than the whole cap still runs, alone)
                if core_cap and not job.local and (core_full[pool] or
                        cores_used[pool] and cores_used[pool] + job.cores > core_cap):
                    core_full[pool] = waiting = True
                    break   # pending until running jobs finish and give their cores back
                if job.vars.get("after"):
                    states = {j.name: j.state for j in task.jobs}
                    if any(states.get(a) not in ("succeeded",) for a in job.vars["after"]):
                        if any(states.get(a) in ("failed", "cancelled") for a in job.vars["after"]):
                            job.state, job.finished = "cancelled", _now()
                            job.failure = {"class": "design", "signature": "dependency step failed"}
                            changed = True
                        continue
                if job.local:
                    host = "localhost"
                else:
                    avoid = {h["host"] for h in job.history if h.get("class") == "infra" and h.get("host")}
                    host = self.placement.choose(job.cores, job.mem_gb, per_host, avoid=avoid,
                                                 pin=task.spec.get("host"))
                    if host is None:
                        break   # cluster full for this task; try again next tick
                job.state, job.host = "launching", host   # reserved; launched below
                starts.append((task, job, host))
                per_host[host] = per_host.get(host, 0) + 1
                running += 1
                if not job.local:
                    cores_used[pool] += job.cores
                changed = True
            label = "build core cap" if pool == "build" else "core cap"
            note = f"pending: featx {label} {core_cap} reached" if waiting else ""
            if task.note != note and (waiting or task.note.startswith(("pending: featx core cap",
                                                                      "pending: featx build core cap"))):
                task.note, changed = note, True
            if changed and not any(t is task for t, _, _ in starts):
                self._refresh_task_state(tree, task)
                self._save(task)
        todo = []
        for task, job, host in starts:
            jdir = self._prepare_job(tree, task, job)
            if jdir is not None:
                todo.append((task, job, host, jdir, job.attempt))
        for task in {id(t): t for t, _, _ in starts}.values():
            # Saved 'launching' before the ssh: a daemon restart then gives the attempt
            # launch_grace to show up instead of launching it a second time.
            self._refresh_task_state(tree, task)
            self._save(task)
        return todo

    def _script_for(self, tree: Tree, task: Task, job: Job, jdir: Path) -> tuple[str, str, str | None]:
        spec = task.spec
        if task.kind == "build":
            return jobgen.build_script(task.id, spec), "log", None
        if task.kind == "run":
            rdir = self._runset_dir(tree, task)
            if spec["executor"] == "perf_trigger":
                (rdir / "run").mkdir(parents=True, exist_ok=True)
                store.atomic_write_text(rdir / "run" / "emu-wrapper.sh", jobgen.perf_trigger_wrapper(spec))
                os.chmod(rdir / "run" / "emu-wrapper.sh", 0o755)
                servers = spec["perf_trigger"].get("servers")
                if not servers:
                    self.placement.refresh()
                    snap = sorted((h for h in self.placement.snapshot(self.jobs_per_host()) if h.get("free_cores", 0) > 8),
                                  key=lambda h: -h["free_cores"])
                    servers = ",".join(h["host"] for h in snap[: int(spec["perf_trigger"].get("n_servers", 6))])
                job.vars["servers"] = servers
                return jobgen.perf_trigger_script(task.id, spec, rdir, servers), "log", None
            point = ptmod.parse(job.name)
            ctx = {"point.name": point["name"], "point.bench": point["bench"], "point.cp": point["cp"],
                   "point.weight": point["weight"], "emu": spec["emu"], "W": spec["W"], "I": spec["I"],
                   "args": spec.get("args", ""), "trace_root": spec.get("trace_root", ""),
                   **presets.point_vars(spec, point["name"])}
            command, inputs = presets.point_command(spec, ctx)
            return (jobgen.run_point_script(task.id, spec, point, command, inputs),
                    "simulator_out.txt", "simulator_err.txt")
        from ..post import plugins
        step = (spec.get("steps") or {})[job.name]
        cmd, env = plugins.render(tree, task, job.name, step, self._runset_dir(tree, task), jdir)
        return jobgen.post_script(task.id, job.name, cmd, env), "log", None

    def _prepare_job(self, tree: Tree, task: Task, job: Job) -> Path | None:
        """New attempt: generate cmd.sh. Returns the job dir, or None if generation failed
        (the job is then failed as a design error)."""
        jdir = self._job_dir(tree, task, job)
        job.attempt += 1
        job.started, job.finished, job.rc = None, None, None
        job.hb_value, job.hb_seen, job.progress = None, None, {}
        try:
            script, out_name, err_name = self._script_for(tree, task, job, jdir)
            executor.prepare(jdir, job.attempt, script, out_name, err_name)
        except Exception as error:  # noqa: BLE001
            job.state, job.finished = "failed", _now()
            job.failure = {"class": "design", "rule": "script", "signature": f"script generation: {error}",
                           "ts": _now()}
            self._emit_failure(tree, task, job, tail=traceback.format_exc()[-3000:])
            return None
        return jdir

    # ----------------------------------------------------------------- poll
    def _poll(self, tree: Tree, task: Task) -> None:
        changed = False
        live = config.settings()["liveness"]
        for job in task.jobs:
            if job.state not in ("launching", "running"):
                continue
            jdir = self._job_dir(tree, task, job)
            st = executor.read_state(jdir, job.attempt)
            now = _now()
            if "exit_code" in st:
                rc = int(st["exit_code"] or -1)
                job.rc = rc
                self._job_exited(tree, task, job, rc, jdir)
                changed = True
                continue
            if job.state == "launching":
                if "start" in st:
                    job.state, job.started = "running", float(st["start"])
                    job.hb_value, job.hb_seen = st.get("heartbeat"), now
                    changed = True
                elif now - (job.launched or now) > live["launch_grace"]:
                    self._job_failed(tree, task, job, None, "featx: launch failed: job never started")
                    changed = True
                continue
            hb = st.get("heartbeat")
            if hb != job.hb_value:
                job.hb_value, job.hb_seen = hb, now
                self._alive.pop((task.id, job.name, job.attempt), None)
            elif now - (job.hb_seen or now) > live["dead_after"]:
                # Probe the host without the lock: queue the pid now, act on the answer next tick.
                key = (task.id, job.name, job.attempt)
                if st.get("pid") and key not in self._alive:
                    self._suspects[key] = (job.host, int(st["pid"]))
                    continue
                alive = self._alive.pop(key, False) if st.get("pid") else False
                if alive:
                    job.hb_seen = now   # heartbeat writer stuck (NFS hiccup) but the job is alive
                else:
                    if alive is None:
                        self.placement.mark_bad(job.host)
                    self._job_failed(tree, task, job, None, f"featx: heartbeat stale on {job.host} (alive={alive})")
                    changed = True
                    continue
            key = (task.id, job.name)
            if task.kind == "run" and now - self._progress_at.get(key, 0) >= PROGRESS_EVERY:
                self._progress_at[key] = now
                if task.spec.get("executor") == "points":
                    prog = eta.parse_progress(jdir / "simulator_out.txt")
                    if prog:
                        job.progress = prog
                else:
                    job.progress = self._perf_trigger_progress(tree, task)
        if self._refresh_task_state(tree, task) or changed:
            self._save(task)
        elif _now() - self._saved_at.get(task.id, 0) >= PROGRESS_EVERY:
            self._save(task)   # progress / heartbeat bookkeeping, throttled

    def _perf_trigger_progress(self, tree: Tree, task: Task) -> dict:
        rdir = self._runset_dir(tree, task) / "run"
        names = ptmod.from_suite(task.spec["perf_trigger"]["json_path"])
        done = 0
        for n in names:
            out = rdir / n / "simulator_out.txt"
            if out.is_file():
                tail = classify.tail_text(out, 4096, skip_perf=False)
                if "EXCEEDING CYCLE/INSTR LIMIT" in tail or "GOOD TRAP" in tail:
                    done += 1
        return {"points_done": done, "points_total": len(names)}

    def _job_exited(self, tree: Tree, task: Task, job: Job, rc: int, jdir: Path) -> None:
        job.finished = _now()
        if rc == 0 and task.kind == "build":
            art = jdir / "emu"
            if not art.is_file():
                self._job_failed(tree, task, job, rc, "featx: artifact missing: build job left no emu")
                return
            key = task.outputs.get("bincache_key") or store.new_id()
            entry = tree.get(task.node_id)
            emu = provenance.bincache_store(key, art, {"key": key, "node": entry.rel, "runset": task.runset_name,
                                                       "task": task.id, "workdir": task.spec.get("workdir"),
                                                       "tool": task.spec.get("tool"), "mode": task.spec.get("mode")})
            art.unlink()
            task.outputs["emu"] = str(emu)
            self._finish_build(tree, task, emu, cached=False)
        if rc == 0 and task.kind == "post":
            if not (jdir / "summary.json").is_file():
                self._job_failed(tree, task, job, rc, "featx: post step wrote no summary.json")
                return
        if rc == 0:
            job.state = "succeeded"
            dur = job.finished - (job.started or job.launched or job.finished)
            self.history.record(eta.history_key(task.kind, job.name, task.spec), dur, True, host=job.host)
            return
        self._job_failed(tree, task, job, rc, "")

    def _job_failed(self, tree: Tree, task: Task, job: Job, rc: int | None, synthetic: str) -> None:
        jdir = self._job_dir(tree, task, job)
        names = ("simulator_out.txt", "simulator_err.txt") if task.kind == "run" and \
            task.spec.get("executor") == "points" else ("log",)
        text = "\n".join(classify.tail_text(jdir / n) for n in names)
        if synthetic:
            text += "\n" + synthetic
        verdict = classify.classify(task.kind, rc, text)
        job.rc, job.finished = rc, _now()
        rec = {"attempt": job.attempt, "host": job.host, "rc": rc, "time": store.now_iso(), **verdict}
        job.history.append(rec)
        max_infra = config.settings()["retry"]["max_infra_attempts"]
        infra_count = sum(1 for h in job.history if h.get("class") == "infra")
        if rc is None:
            # No exit_code: featx-exec died or the host went away. Kill whatever survived (the
            # emu, under `timeout`, leaves the process group) whether or not we retry.
            st = executor.read_state(jdir, job.attempt)
            if job.host and st.get("pid"):
                self._queue_kill(job.host, st["pid"], "KILL")
        if verdict["class"] == "infra" and infra_count <= max_infra:
            if job.host:
                self.placement.mark_bad(job.host, 120)
            job.state, job.host = "queued", None
            self.inbox.emit("retry", task=task.id, job=job.name, node=self._rel(tree, task),
                            runset=task.runset_name, signature=verdict["signature"], attempt=job.attempt)
            return
        job.state = "failed"
        job.failure = {**verdict, "rc": rc, "time": store.now_iso(), "ts": _now()}
        prog = eta.parse_progress(jdir / "simulator_out.txt") if task.kind == "run" else None
        if prog:
            job.failure["inst_reached"] = prog["inst"]
        self._emit_failure(tree, task, job, tail=classify.last_lines(text, 80))

    def _emit_failure(self, tree: Tree, task: Task, job: Job, tail: str) -> None:
        jdir = self._job_dir(tree, task, job)
        bundle = {"task": task.id, "kind": task.kind, "job": job.name, "node": self._rel(tree, task),
                  "node_id": task.node_id, "runset": task.runset_name, "job_dir": str(jdir),
                  "host": job.host, **(job.failure or {}), "attempt": job.attempt}
        store.write_json(jdir / "failure.json", {**bundle, "tail": tail})
        bundle["tail"] = tail[-6000:]
        bundle["hint"] = ("design failure: inspect node_context/task_log, append problem-analysis "
                          "(problem_log_append) then task_relaunch or resubmit")
        self.inbox.emit("failure", **bundle)

    def _refresh_task_state(self, tree: Tree, task: Task) -> bool:
        if task.state != "running":
            return False
        states = [j.state for j in task.jobs]
        if any(s in JOB_ACTIVE for s in states):
            return False
        old = task.state
        task.finished = _now()
        failed = [j for j in task.jobs if j.state == "failed"]
        if not failed and all(s == "succeeded" for s in states):
            task.state, task.failure = "succeeded", None
        elif not failed:   # user-cancelled (possibly after some jobs succeeded)
            task.state = "cancelled"
        else:
            task.state = "failed"
            sigs: dict[str, int] = {}
            for j in failed:
                s = (j.failure or {}).get("signature", "?")
                sigs[s] = sigs.get(s, 0) + 1
            task.failure = {"class": "design", "failed_jobs": len(failed), "signatures": sigs,
                            "signature": max(sigs, key=sigs.get) if sigs else "cancelled dependency",
                            "ts": _now()}
        rel = self._rel(tree, task)
        self.inbox.emit("task_done", task=task.id, kind=task.kind, node=rel, runset=task.runset_name,
                        state=task.state, counts=task.counts(), failure=task.failure)
        self._maybe_seal(tree, task)
        return old != task.state

    def _maybe_seal(self, tree: Tree, task: Task) -> None:
        group = [t for t in self.tasks.values() if t.runset_id == task.runset_id]
        if all(t.state in TERMINAL_TASK_STATES for t in group) and all(t.state == "succeeded" for t in group):
            try:
                entry = tree.get(task.node_id)
                _, rs = tree.find_runset(f"{entry.rel}/{task.runset_name}")
                rs.sealed = True
                tree.save_runset(entry, rs)
                self.inbox.emit("runset_done", node=entry.rel, runset=rs.name,
                                post=sorted(p.name for p in (entry.dir / rs.name / "post").iterdir())
                                if (entry.dir / rs.name / "post").is_dir() else [])
            except TreeError:
                pass

    # ============================================================== control
    def get(self, ref: str) -> Task:
        if ref in self.tasks:
            return self.tasks[ref]
        hits = [t for i, t in self.tasks.items() if i.startswith(ref.upper())]
        if len(hits) == 1:
            return hits[0]
        raise SchedError(f"no such task: {ref}" if not hits else f"ambiguous task prefix {ref}")

    def cancel(self, ref: str, jobs: list[str] | None = None) -> dict:
        """Cancel some jobs, or (jobs=None) the whole task plus the not-yet-started tasks
        that depend on it."""
        with self.lock:
            task = self.get(ref)
            tree = self._tree()
            n = self._cancel_jobs(tree, task, jobs)
            cascaded = []
            if not jobs:
                if task.state in ACTIVE_TASK_STATES:   # queued (no jobs yet) or emptied running
                    task.state, task.finished, task.note = "cancelled", _now(), "cancelled"
                for other in self.tasks.values():
                    if task.id in other.after and other.state == "queued":
                        other.state, other.finished, other.note = "cancelled", _now(), f"cancelled with {task.id[:12]}"
                        self._save(other)
                        cascaded.append(other.id)
            self._save(task)
            self.inbox.emit("cancelled", task=task.id, node=self._rel(tree, task), runset=task.runset_name,
                            jobs=jobs, dependents=cascaded)
            out = {"task": task.id, "cancelled_jobs": n, "state": task.state, "dependents": cascaded}
        self._do_ssh_work(blocking=False)   # the kills, without the lock (else the next tick)
        return out

    def _cancel_jobs(self, tree: Tree, task: Task, jobs: list[str] | None) -> int:
        n = 0
        for job in task.jobs:
            if jobs and job.name not in jobs or job.state not in JOB_ACTIVE:
                continue
            if job.state in ("launching", "running") and job.host:
                st = executor.read_state(self._job_dir(tree, task, job), job.attempt)
                if st.get("pid"):
                    self._queue_kill(job.host, st["pid"])
            job.state, job.finished = "cancelled", _now()
            n += 1
        self._refresh_task_state(tree, task)
        return n

    def relaunch(self, ref: str, *, jobs: list[str] | None = None, force: bool = False,
                 reason: str = "") -> dict:
        """Re-queue failed/cancelled jobs of a task in place (same runset, same spec).
        Design failures need a problem-analysis.log entry newer than the failure, and are
        limited by max_design_relaunches (then `unblock` is required)."""
        with self.lock:
            task = self.get(ref)
            tree = self._tree()
            rdir = self._runset_dir(tree, task)
            if task.state in ACTIVE_TASK_STATES and not any(j.state in ("failed", "cancelled") for j in task.jobs):
                raise SchedError(f"task {task.id} is {task.state}; nothing to relaunch")
            targets = [j for j in task.jobs if j.state in ("failed", "cancelled") and (not jobs or j.name in jobs)]
            if not task.jobs and task.state in ("failed", "cancelled"):
                targets = []
            design = [j for j in targets if (j.failure or {}).get("class") == "design"]
            fail_ts = max([(j.failure or {}).get("ts", 0) for j in design] + [(task.failure or {}).get("ts", 0)
                                                                             if task.state == "failed" else 0])
            if (design or (task.state == "failed" and not task.jobs)) and not force:
                if task.blocked or task.design_relaunches >= task.max_design_relaunches:
                    task.blocked = True
                    self._save(task)
                    self.inbox.emit("budget_exhausted", task=task.id, node=self._rel(tree, task),
                                    runset=task.runset_name, relaunches=task.design_relaunches)
                    raise SchedError(f"design relaunch budget exhausted ({task.design_relaunches}/"
                                     f"{task.max_design_relaunches}); a human must run `featx task unblock {task.id}`")
                if not problemlog.analysed_since(rdir, fail_ts):
                    raise SchedError("relaunch refused: append a problem-analysis entry for this failure first "
                                     f"(featx log append {self._rel(tree, task)}/{task.runset_name} ...)")
            if design or (task.state == "failed" and not task.jobs):
                task.design_relaunches += 1
            recipes.snapshot_scripts(tree, tree.get(task.node_id), rdir)
            if task.kind == "build" and targets:
                # The fix being relaunched (worktree edit or script) changes what gets built:
                # store the new binary under its own key, not the failed source's key.
                task.outputs["bincache_key"] = self._build_key(task, rdir)
            for job in targets:
                job.state, job.failure, job.rc, job.host = "queued", None, None, None
            if not task.jobs:
                task.state = "queued"
            elif targets:
                task.state = "running"
            task.failure, task.finished = None, None
            # Dependents that were blocked by this task's failure resume automatically.
            for other in self.tasks.values():
                if task.id in other.after and other.state == "failed" and not other.jobs:
                    other.state, other.failure = "queued", None
                    self._save(other)
            self._unseal(tree, task)
            self._save(task)
            self.inbox.emit("relaunched", task=task.id, node=self._rel(tree, task), runset=task.runset_name,
                            jobs=[j.name for j in targets], reason=reason)
            return {"task": task.id, "requeued": [j.name for j in targets], "state": task.state,
                    "design_relaunches": task.design_relaunches}

    def _unseal(self, tree: Tree, task: Task) -> None:
        entry = tree.get(task.node_id)
        _, rs = tree.find_runset(f"{entry.rel}/{task.runset_name}")
        if rs.sealed:
            rs.sealed = False
            tree.save_runset(entry, rs)

    def resubmit(self, ref: str, *, only_failed: bool = True, force: bool = False,
                 overrides: dict | None = None, kinds: list[str] | None = None, purpose: str = "",
                 bin_ref: str | None = None) -> dict:
        """New runset derived from the task's runset, re-resolving recipes (after an LLM fix).
        With only_failed, the run covers only the points that failed."""
        with self.lock:
            task = self.get(ref)
            tree = self._tree()
            rdir = self._runset_dir(tree, task)
            fail_ts = (task.failure or {}).get("ts") or max([(j.failure or {}).get("ts", 0) for j in task.jobs] + [0])
            if task.state == "failed" and not force and not problemlog.analysed_since(rdir, fail_ts):
                raise SchedError("resubmit refused: append a problem-analysis entry for this failure first")
            group = [t for t in self.tasks.values() if t.runset_id == task.runset_id]
            kinds = kinds or [t.kind for t in sorted(group, key=lambda t: ("build", "run", "post").index(t.kind))]
            only = None
            run_task = next((t for t in group if t.kind == "run"), None)
            if only_failed and run_task and run_task.spec.get("executor") == "points":
                only = [j.name for j in run_task.jobs if j.state != "succeeded"] or None
            # Without a rebuild, keep the binary the source runset ran (e.g. one given via --bin;
            # re-resolving the recipe would fall back to ${bin} of the new, empty runset).
            bin_ref = bin_ref or (run_task.spec.get("emu") if run_task and "build" not in kinds else None)
            entry = tree.get(task.node_id)
            res = self.submit(entry.node.id, kinds, purpose=purpose or f"resubmit of {task.runset_name}",
                              overrides=overrides, derived_from=f"{entry.rel}/{task.runset_name}",
                              only_points=only, bin_ref=bin_ref,
                              priority=max(t.priority for t in group))
            # The derived runset supersedes the source: drop its tasks still waiting on the failure.
            res["superseded"] = []
            for t in group:
                if t.state == "queued":
                    t.state, t.finished, t.note = "cancelled", _now(), f"superseded by {res['runset']}"
                    self._save(t)
                    res["superseded"].append(t.id)
            return res

    def set_priority(self, ref: str, priority: int, *, runset: bool = False) -> dict:
        """Change the launch priority of a task (or every task of its runset). Running jobs are
        not preempted; the order decides which queued jobs get freed cores first."""
        with self.lock:
            task = self.get(ref)
            group = [t for t in self.tasks.values() if t.runset_id == task.runset_id] if runset else [task]
            for t in group:
                t.priority = int(priority)
                self._save(t)
            return {"tasks": [t.id for t in group], "priority": int(priority)}

    def unblock(self, ref: str, extra: int = 1) -> dict:
        with self.lock:
            task = self.get(ref)
            task.blocked = False
            task.max_design_relaunches = task.design_relaunches + extra
            self._save(task)
            return {"task": task.id, "max_design_relaunches": task.max_design_relaunches}

    # ================================================================ views
    def task_view(self, task: Task, tree: Tree | None = None, *, jobs: bool = False) -> dict:
        tree = tree or self._tree()
        out = {"id": task.id, "kind": task.kind, "node": self._rel(tree, task), "runset": task.runset_name,
               "state": task.state, "note": task.note, "after": task.after, "counts": task.counts(),
               "failure": task.failure, "created": task.created, "blocked": task.blocked,
               "priority": task.priority,
               "design_relaunches": task.design_relaunches, "outputs": task.outputs,
               "eta_s": self.task_eta(task)}
        prog = [j.progress for j in task.jobs if j.state == "running" and j.progress.get("inst")]
        if prog and task.spec.get("I"):
            out["progress_pct"] = round(100 * sum(p["inst"] for p in prog) / (len(prog) * int(task.spec["I"])), 1)
        if task.kind == "run" and task.spec.get("I"):
            out["target_inst"] = int(task.spec["I"])
        if task.kind == "run" and task.spec.get("executor") == "perf_trigger" and task.jobs:
            out["perf_trigger"] = task.jobs[0].progress
        if jobs:
            out["jobs"] = [{"name": j.name, "state": j.state, "host": j.host, "attempt": j.attempt, "rc": j.rc,
                            "progress": j.progress, "failure": j.failure,
                            "eta_s": self.job_eta(task, j)} for j in task.jobs]
        return out

    def job_eta(self, task: Task, job: Job) -> float | None:
        if job.state in ("succeeded", "failed", "cancelled"):
            return 0.0
        target = int(task.spec["I"]) if task.kind == "run" and task.spec.get("I") else None
        fb = self.history.median(eta.history_key(task.kind, job.name, task.spec))
        if fb is None and task.kind == "run":
            fb = self.history.generic_run()
        if job.state == "queued":
            return fb
        return eta.job_remaining(job.progress, target, job.started, fb)

    def task_eta(self, task: Task) -> float | None:
        """Remaining seconds including the critical path through unfinished dependencies."""
        if task.state in TERMINAL_TASK_STATES:
            return 0.0
        own = self._own_eta(task)
        deps = [self.task_eta(self.tasks[d]) for d in task.after if d in self.tasks]
        dep_eta = max([d for d in deps if d is not None], default=0.0)
        if own is None:
            return None
        return own + dep_eta

    def _own_eta(self, task: Task) -> float | None:
        if not task.jobs:
            key = eta.history_key(task.kind, "build" if task.kind == "build" else "?", task.spec)
            if task.kind == "run":
                n = len(ptmod.resolve(task.spec)) if task.spec.get("executor") == "points" else 1
                per = self.history.generic_run()
                return per * max(1.0, math.ceil(n / 200)) if per else None
            return self.history.median(key) or (3 * 3600 if task.kind == "build" else 300)
        running = [self.job_eta(task, j) for j in task.jobs if j.state in ("launching", "running")]
        queued = [self.job_eta(task, j) for j in task.jobs if j.state == "queued"]
        if any(r is None for r in running + queued):
            known = [r for r in running + queued if r is not None]
            if not known:
                return None
        running = [r or 0 for r in running]
        queued = [q or 0 for q in queued]
        slots = max(len(running), 1)
        return max(running, default=0.0) + (sum(queued) / slots if queued else 0.0)

    def list_tasks(self, *, active_only: bool = False, node_prefix: str | None = None,
                   limit: int = 200) -> list[dict]:
        tree = self._tree()
        out = []
        for t in sorted(self.tasks.values(), key=lambda t: t.created, reverse=True):
            if active_only and t.state not in ACTIVE_TASK_STATES:
                continue
            v = self.task_view(t, tree)
            if node_prefix and not v["node"].startswith(node_prefix):
                continue
            out.append(v)
            if len(out) >= limit:
                break
        return out

    def node_status(self) -> dict[str, dict]:
        """Per node id: latest runset task states + active stage + eta (for the tree view)."""
        out: dict[str, dict] = {}
        for t in sorted(self.tasks.values(), key=lambda t: t.created):
            d = out.setdefault(t.node_id, {"runset": None, "tasks": {}, "active": [], "eta_s": 0.0})
            if d["runset"] != t.runset_name:
                d["runset"], d["tasks"] = t.runset_name, {}
            d["tasks"][t.kind] = t.state
            if t.state in ACTIVE_TASK_STATES:
                d["active"].append(t.kind)
                e = self.task_eta(t)
                d["eta_s"] = None if e is None or d["eta_s"] is None else max(d["eta_s"], e)
        return out

    def log_path(self, ref: str, job: str | None = None, stream: str = "out") -> Path:
        task = self.get(ref)
        tree = self._tree()
        if not task.jobs:
            raise SchedError("task has no jobs yet")
        j = next((x for x in task.jobs if x.name == job), None) if job else (
            next((x for x in task.jobs if x.state == "failed"), None) or
            next((x for x in task.jobs if x.state == "running"), None) or task.jobs[0])
        if j is None:
            raise SchedError(f"no job {job} in task {task.id}")
        jdir = self._job_dir(tree, task, j)
        if task.kind == "run" and task.spec.get("executor") == "points":
            return jdir / ("simulator_err.txt" if stream == "err" else "simulator_out.txt")
        return jdir / "log"
