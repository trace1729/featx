"""featx CLI. Tree/recipe/worktree/storage commands work directly on the data root (the
daemon is not required); scheduler commands (submit, ps, logs, events, relaunch...) go
through featxd."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

import typer
import yaml

from . import config, docs, files, leaderboard, service, store
from .lang import tr
from .client import ApiError, Client, DaemonDown, daemon_alive, daemon_info
from .sched.eta import fmt_duration
from .tree import Tree, TreeError

app = typer.Typer(no_args_is_help=True, add_completion=False, help=tr("featx CLI"))
node_app = typer.Typer(no_args_is_help=True, help=tr("Feature-tree nodes (category/feature/subfeature)."))
recipe_app = typer.Typer(no_args_is_help=True, help=tr("build/run/post recipes (inherited down the tree)."))
wt_app = typer.Typer(no_args_is_help=True, help=tr("Per-node git worktrees."))
task_app = typer.Typer(no_args_is_help=True, help=tr("Scheduler tasks."))
daemon_app = typer.Typer(no_args_is_help=True, help=tr("featxd control."))
log_app = typer.Typer(no_args_is_help=True, help=tr("problem-analysis.log entries."))
base_app = typer.Typer(no_args_is_help=True, help=tr("Named baselines (reference result roots)."))
bin_app = typer.Typer(no_args_is_help=True, help=tr("Binary cache."))
script_app = typer.Typer(no_args_is_help=True, help="Per-node helper scripts.")
config_app = typer.Typer(no_args_is_help=True, help=tr("featx.yaml (user layer over the package defaults)."))
plugin_app = typer.Typer(no_args_is_help=True, help=tr("Post-processing plugins (manifests)."))
ckpt_app = typer.Typer(no_args_is_help=True, help=tr("SimPoint checkpoint sets for exec runs (run.checkpoint)."))
for name, sub in (("node", node_app), ("recipe", recipe_app), ("worktree", wt_app), ("task", task_app),
                  ("daemon", daemon_app), ("log", log_app), ("baseline", base_app), ("bin", bin_app),
                  ("script", script_app), ("config", config_app), ("plugin", plugin_app),
                  ("ckpt", ckpt_app)):
    app.add_typer(sub, name=name)

KINDS = ("build", "run", "post")


@app.callback()
def global_options(lang: Optional[str] = typer.Option(None, "--lang", help="output language: en or zh")):
    """Select the language for help and human-readable output."""
    if lang:
        if lang.lower() not in {"en", "zh"}:
            raise typer.BadParameter("language must be en or zh")
        os.environ["FEATX_LANG"] = lang.lower()


def out(obj) -> None:
    if isinstance(obj, str):
        typer.echo(obj)
    else:
        typer.echo(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def fail(msg: str, code: int = 1):
    typer.secho(f"featx: {tr(msg)}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def client() -> Client:
    try:
        return Client()
    except DaemonDown as error:
        fail(str(error))


def guarded(fn):
    """Turn user-facing errors into one red line instead of a traceback."""
    import functools

    @functools.wraps(fn)
    def wrap(*a, **kw):
        try:
            return fn(*a, **kw)
        except (TreeError, ApiError, DaemonDown, KeyError, ValueError, FileNotFoundError, TimeoutError,
                PermissionError, config.ConfigError) as error:
            fail(str(error).strip("'\""))
    return wrap


def via_daemon() -> Client | None:
    """Use the daemon for tree mutations when it is up (it serializes with the scheduler);
    otherwise operate on files directly."""
    if os.environ.get("FEATX_DIRECT") or not daemon_alive():
        return None
    try:
        return Client()
    except DaemonDown:
        return None


def parse_kv(items: list[str]) -> dict:
    """a.b=1 c=[x,y] -> nested dict, values parsed as YAML."""
    res: dict = {}
    for item in items or []:
        key, sep, val = item.partition("=")
        if not sep:
            fail(f"bad override {item!r} (want key=value)")
        cur = res
        parts = key.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = yaml.safe_load(val) if val != "" else ""
    return res


# ===================================================================== tree
@app.command()
@guarded
def tree(root: Optional[str] = typer.Argument(None, help="subtree root"),
         as_json: bool = typer.Option(False, "--json")):
    """Feature tree with verdicts and the latest runset's task states / ETA."""
    c = via_daemon()
    if c:
        out(c.get("/tree", fmt="rows" if as_json else "text", root=root))
        return
    t = Tree()
    out(service.tree_rows(t) if as_json else service.tree_text(t, None, root) or "(empty tree)")


@node_app.command("add")
@guarded
def node_add(path: str, hypothesis: str = typer.Option("", "-H", "--hypothesis"),
             tag: List[str] = typer.Option([], "--tag"), parents: bool = typer.Option(False, "-p", "--parents"),
             worktree: bool = typer.Option(False, "-w", "--worktree", help="also create the node worktree"),
             base: Optional[str] = typer.Option(None, help="worktree base ref (default git.base)")):
    """Create a node, e.g. `featx node add wpe/real/after-anchor -p -H '...' -w`."""
    c = via_daemon()
    body = {"path": path, "hypothesis": hypothesis, "tags": tag, "parents": parents, "worktree": worktree,
            "base": base}
    out(c.post("/nodes", body, timeout=600) if c else
        service.node_add(path, hypothesis=hypothesis, tags=tag, parents=parents, worktree_create=worktree,
                         base=base))


@node_app.command("show")
@guarded
def node_show(ref: str, runsets: int = typer.Option(3, help="latest N runsets")):
    """Node context bundle (what the MCP node_context tool returns)."""
    out(service.node_context(ref, runsets))


@node_app.command("set")
@guarded
def node_set(ref: str, hypothesis: Optional[str] = typer.Option(None, "-H", "--hypothesis"),
             verdict: Optional[str] = typer.Option(None, help="open|supported|refuted|inconclusive"),
             note: Optional[str] = typer.Option(None, "--verdict-note"),
             status: Optional[str] = typer.Option(None, help="active|parked|done|abandoned"),
             evidence: List[str] = typer.Option([], "--evidence", help="runset ref backing the verdict"),
             rm_evidence: List[str] = typer.Option([], "--rm-evidence", help="drop an evidence entry"),
             tag: List[str] = typer.Option([], "--tag"), untag: List[str] = typer.Option([], "--untag"),
             notes: Optional[str] = typer.Option(None, "--notes"),
             comment: Optional[str] = typer.Option(None, "--comment", help="free-form thoughts")):
    """Update hypothesis / verdict / status / evidence / tags / notes / comment."""
    fields = {"hypothesis": hypothesis, "verdict": verdict, "verdict_note": note, "status": status,
              "add_evidence": evidence or None, "rm_evidence": rm_evidence or None,
              "add_tags": tag or None, "rm_tags": untag or None, "notes": notes, "comment": comment}
    fields = {k: v for k, v in fields.items() if v is not None}
    c = via_daemon()
    out(c.post("/nodes/update", {"ref": ref, **fields}) if c else service.node_update(ref, **fields))


@node_app.command("mv")
@guarded
def node_mv(src: str, dst: str, parents: bool = typer.Option(False, "-p", "--parents"),
            keep_branches: bool = typer.Option(False, help="do not rename the featx-created branches"),
            dry_run: bool = typer.Option(False, "-n", "--dry-run", help="run the checks and print the plan only")):
    """Cascade move: node dirs (+forwarding symlink), worktrees, branches. Refused while
    tasks under the subtree are active."""
    c = via_daemon()
    body = {"src": src, "dst": dst, "parents": parents, "rename_branches": not keep_branches,
            "dry_run": dry_run}
    out(c.post("/nodes/mv", body, timeout=600) if c else
        service.node_mv(src, dst, parents=parents, rename_branches=not keep_branches, dry_run=dry_run))


@node_app.command("rm")
@guarded
def node_rm(ref: str, force: bool = typer.Option(False, "-f", "--force"),
            drop_worktrees: bool = typer.Option(False, help="also delete worktrees (diffs saved to trash) "
                                                "and featx branches without own commits"),
            drop_branches: bool = typer.Option(False, help="with --drop-worktrees: delete branches with "
                                               "unmerged commits too")):
    """Soft delete into tree/.trash (featx gc --apply purges)."""
    c = via_daemon()
    body = {"ref": ref, "force": force, "drop_worktrees": drop_worktrees, "drop_branches": drop_branches}
    out(c.post("/nodes/rm", body) if c else
        service.node_rm(ref, force=force, drop_worktrees=drop_worktrees, drop_branches=drop_branches))


@node_app.command("runsets")
@guarded
def node_runsets(ref: str):
    """All runsets of a node with task states and post summaries."""
    for rs in service.runsets(ref):
        tasks = " ".join(f"{t['kind']}:{t['state']}" for t in rs["tasks"])
        seal = "sealed" if rs["sealed"] else "open"
        typer.echo(f"{rs['ref']:50s} {seal:7s} {tasks}  {rs['purpose']}")


# ================================================================== recipes
@recipe_app.command("show")
@guarded
def recipe_show(ref: str, kind: str = typer.Argument(..., help="build|run|post"),
                own: bool = typer.Option(False, "--own", help="only this node's layer"),
                explain: bool = typer.Option(False, help="show which layer set each key")):
    """Effective recipe after inheritance (defaults -> $ROOT/recipes -> ancestors -> node)."""
    r = service.recipe_get(ref, kind, effective=not own, explain=explain)
    out(r if isinstance(r, str) else yaml.safe_dump(r, sort_keys=False, allow_unicode=True))


@recipe_app.command("set")
@guarded
def recipe_set(ref: str, kind: str, file: Optional[Path] = typer.Option(None, "-f", "--file"),
               kv: List[str] = typer.Argument(None, help="key.path=value overrides merged into the node layer")):
    """Replace the node's recipe layer from a YAML file, or merge key=value pairs."""
    if file:
        data = yaml.safe_load(file.read_text()) or {}
        merge = False
    else:
        data, merge = parse_kv(kv), True
    c = via_daemon()
    res = c.post("/recipes", {"ref": ref, "kind": kind, "data": data, "merge": merge}) if c else \
        service.recipe_set(ref, kind, data, merge=merge)
    out(yaml.safe_dump(res, sort_keys=False))


@recipe_app.command("edit")
@guarded
def recipe_edit(ref: str, kind: str):
    """Open the node's own recipe layer in $EDITOR."""
    entry = Tree().get(ref)
    path = entry.dir / "recipes" / f"{kind}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# {kind} recipe layer for {entry.rel} (merged over inherited layers)\n")
    subprocess.call([os.environ.get("EDITOR", "vi"), str(path)])
    yaml.safe_load(path.read_text())
    out(str(path))


# ================================================================= worktree
@wt_app.command("create")
@guarded
def worktree_create(ref: str, base: Optional[str] = None, branch: Optional[str] = None,
                    path: Optional[str] = None, repo: Optional[str] = None,
                    sub: List[str] = typer.Option(None, "--sub", help="submodule worktrees (default TraceRTL)"),
                    adopt: bool = typer.Option(False, help="register an existing worktree at --path")):
    """Create the node worktree at <wt_root>/<node path> on branch <node path>
    (git.branch_prefix + path; refused when that name clashes with an existing branch — pass --branch)."""
    kw = {"base": base, "branch": branch, "path": path, "repo": repo, "submodules": sub or None, "adopt": adopt}
    c = via_daemon()
    out(c.post("/worktrees", {"ref": ref, **kw}, timeout=900) if c else service.worktree_create(ref, **kw))


@wt_app.command("rename-branch")
@guarded
def worktree_rename_branch(ref: str, to: Optional[str] = typer.Option(None, help="new name (default: the "
                                                                          "node path, git.branch_prefix + path)"),
                           dry_run: bool = typer.Option(False, "--dry-run")):
    """Rename the node's featx-created branch (and its submodule worktree branches), e.g. an
    old featx/<path> branch to <path>. Adopted user branches are refused."""
    out(service.worktree_rename_branch(ref, to, dry_run=dry_run))


@wt_app.command("status")
@guarded
def worktree_status(ref: str):
    out(service.worktree_status(ref))


# ================================================================== scripts
@script_app.command("write")
@guarded
def script_write(ref: str, name: str, file: Path = typer.Option(..., "-f", "--file")):
    out(service.script_write(ref, name, file.read_text()))


@script_app.command("show")
@guarded
def script_show(ref: str, name: str):
    out(service.script_read(ref, name))


# ================================================================== submit
def _submit(node: str, kinds: list[str], runset, purpose, bin_, set_, points, steps, after, priority=0):
    overrides: dict = {}
    for item in set_ or []:
        kind, dot, rest = item.partition(".")
        if kind not in KINDS or not dot:
            fail(f"--set must look like run.W=1000000 (got {item!r})")
        overrides = config.deep_merge(overrides, {kind: parse_kv([rest])})
    body = {"node": node, "kinds": kinds, "runset": runset, "purpose": purpose, "overrides": overrides,
            "bin": bin_, "points": points or None, "post_steps": steps or None, "after": after or None,
            "priority": priority}
    res = client().post("/tasks", body, timeout=300)
    typer.echo(f"runset {res['runset']}")
    for t in res["tasks"]:
        typer.echo(f"  {t['kind']:5s} {t['id']}" + (f"  after {','.join(a[:10] for a in t['after'])}"
                                                    if t["after"] else ""))


SUBMIT_HELP = "--set kind.key=value overrides, e.g. --set run.W=1000000 --set run.points=[a_1_0.1]"


@app.command()
@guarded
def submit(node: str, kinds: List[str] = typer.Argument(None, help="build run post (default: all three)"),
           runset: Optional[str] = typer.Option(None, help="reuse an unsealed runset (YYYYMMDD-n)"),
           purpose: str = typer.Option("", "-m", "--purpose"),
           bin_: Optional[str] = typer.Option(None, "--bin", help="emu file | bincache key | runset ref"),
           set_: List[str] = typer.Option([], "--set", help=SUBMIT_HELP),
           point: List[str] = typer.Option([], "--point", help="restrict run to these points"),
           step: List[str] = typer.Option([], "--step", help="restrict post to these steps"),
           after: List[str] = typer.Option([], "--after", help="task ids the first task waits for"),
           priority: int = typer.Option(0, "--priority", help="higher launches first when cores are scarce")):
    """Submit build/run/post as a chained DAG in a new runset of NODE."""
    for k in kinds or []:
        if k not in KINDS:
            fail(f"unknown kind {k} (build|run|post)")
    _submit(node, list(kinds or KINDS), runset, purpose, bin_, set_, point, step, after, priority)


@app.command()
@guarded
def ps(all_: bool = typer.Option(False, "-a", "--all", help="include finished tasks"),
       node: Optional[str] = typer.Option(None, help="node path prefix"),
       limit: int = 40, as_json: bool = typer.Option(False, "--json")):
    """Tasks with stage, job counts, progress and ETA."""
    rows = client().get("/tasks", active=not all_, node=node, limit=limit)
    if as_json:
        out(rows)
        return
    if not rows:
        typer.echo(tr("(no tasks)" if all_ else "(no active tasks; -a for all)"))
        return
    typer.echo(f"{'TASK':12s} {'KIND':5s} {'STATE':9s} {'JOBS':22s} {'PROG':>6s} {'ETA':>8s}  NODE/RUNSET")
    for r in rows:
        c = r["counts"]
        jobs = " ".join(f"{k[:4]}={v}" for k, v in sorted(c.items()))
        prog = f"{r['progress_pct']:.0f}%" if r.get("progress_pct") is not None else ""
        eta = fmt_duration(r["eta_s"]) if r["state"] in ("queued", "running") else ""
        state = r["state"] + ("!" if r.get("blocked") else "")
        typer.echo(f"{r['id'][:12]:12s} {r['kind']:5s} {state:9s} {jobs[:22]:22s} {prog:>6s} {eta:>8s}  "
                   f"{r['node']}/{r['runset']}")
        if r.get("failure"):
            typer.secho(f"{'':12s} └ {r['failure'].get('signature')}", fg=typer.colors.RED)
        elif r.get("note"):
            typer.echo(f"{'':12s} └ {r['note']}")


@task_app.command("show")
@guarded
def task_show(tid: str, spec: bool = typer.Option(False, "--spec", help="include the resolved recipe")):
    out(client().get(f"/tasks/{tid}", spec=spec))


@app.command()
@guarded
def logs(tid: str, job: Optional[str] = typer.Option(None, "-j", "--job"),
         err: bool = typer.Option(False, "--err", help="stderr (simulator_err.txt)"),
         lines: int = typer.Option(80, "-n"), follow: bool = typer.Option(False, "-f", "--follow")):
    """Tail a job log (default: first failed, else first running job)."""
    c = client()
    res = c.get(f"/tasks/{tid}/log", job=job, stream="err" if err else "out", lines=lines)
    typer.secho(f"== {res['path']}", fg=typer.colors.BLUE, err=True)
    if not follow:
        typer.echo(res["tail"])
        return
    os.execvp("tail", ["tail", "-n", str(lines), "-F", res["path"]])


@app.command()
@guarded
def eta():
    """Active tasks with remaining time."""
    for r in client().get("/eta"):
        prog = f"{r['progress_pct']:.0f}%" if r.get("progress_pct") is not None else "-"
        typer.echo(f"{r['id'][:12]} {r['kind']:5s} {r['state']:8s} {prog:>5s} ETA {fmt_duration(r['eta_s']):>8s}"
                   f"  {r['node']}/{r['runset']}")


@task_app.command("relaunch")
@guarded
def task_relaunch(tid: str, job: List[str] = typer.Option([], "-j", "--job"),
                  force: bool = typer.Option(False, help="skip the problem-log guardrail"),
                  reason: str = typer.Option("", "-m")):
    """Re-queue failed jobs in place (needs a problem-analysis entry for design failures)."""
    out(client().post(f"/tasks/{tid}/relaunch", {"jobs": job or None, "force": force, "reason": reason}))


@task_app.command("resubmit")
@guarded
def task_resubmit(tid: str, all_points: bool = typer.Option(False, "--all-points"),
                  set_: List[str] = typer.Option([], "--set", help=SUBMIT_HELP),
                  force: bool = typer.Option(False), purpose: str = typer.Option("", "-m")):
    """New runset derived from this task's runset (recipes re-resolved, e.g. after a fix)."""
    overrides: dict = {}
    for item in set_:
        kind, _, rest = item.partition(".")
        overrides = config.deep_merge(overrides, {kind: parse_kv([rest])})
    out(client().post(f"/tasks/{tid}/resubmit", {"only_failed": not all_points, "overrides": overrides,
                                                 "force": force, "purpose": purpose}))


@task_app.command("cancel")
@guarded
def task_cancel(tid: str, job: List[str] = typer.Option([], "-j", "--job")):
    out(client().post(f"/tasks/{tid}/cancel", {"jobs": job or None}))


@task_app.command("priority")
@guarded
def task_priority(tid: str, priority: int = typer.Argument(..., help="higher launches first"),
                  only_task: bool = typer.Option(False, "--only-task", help="not the whole runset")):
    """Change the launch priority of a task's runset (running jobs are not preempted)."""
    out(client().post(f"/tasks/{tid}/priority", {"priority": priority, "runset": not only_task}))


@task_app.command("unblock")
@guarded
def task_unblock(tid: str, extra: int = typer.Option(1, help="additional design relaunches allowed")):
    """Human override after the design-relaunch budget is exhausted."""
    out(client().post(f"/tasks/{tid}/unblock", {"extra": extra}))


# ================================================================== events
@app.command()
@guarded
def events(since: int = 0, all_: bool = typer.Option(False, "-a", "--all", help="include acked"),
           types: Optional[str] = typer.Option(None, help="comma list: failure,task_done,retry,..."),
           node: Optional[str] = None, wait: float = typer.Option(0, help="block up to N s for new events"),
           ack: bool = typer.Option(False, help="ack what was printed"), as_json: bool = typer.Option(False, "--json")):
    """Event inbox (failures, completions, retries, budget exhaustion)."""
    c = client()
    evs = c.get("/events", since=since, unacked=not all_, types=types, node=node, wait=wait,
                timeout=wait + 30 if wait else None)
    if as_json:
        out(evs)
    else:
        for e in evs:
            what = e.get("signature") or e.get("state") or e.get("note") or ""
            where = f"{e.get('node', '')}/{e.get('runset', '')}".strip("/")
            job = f" job={e['job']}" if e.get("job") else ""
            typer.echo(f"#{e['seq']:<5d} {e['time'][5:19]} {e['type']:16s} {where}{job}  {what}")
    if ack and evs:
        c.post("/events/ack", {"seqs": [e["seq"] for e in evs]})


@app.command()
@guarded
def wait(tasks: List[str] = typer.Argument(..., metavar="TASK...", help="task ids returned by featx submit"),
         timeout: float = typer.Option(86400, "--timeout", min=0, help="maximum seconds to wait (0 = no wait)"),
         as_json: bool = typer.Option(False, "--json")):
    """Wait on task events; failed dependencies return as blocked instead of timing out."""
    from .waiting import wait_tasks
    result = wait_tasks(client(), tasks, timeout)
    if as_json:
        out(result)
    else:
        for tid in tasks:
            if tid in result["finished"]:
                view = result["finished"][tid]
                typer.echo(f"{tid} {view['state']} {view.get('counts', {})}")
            elif tid in result["blocked"]:
                deps = ', '.join(result['blocked'][tid]['failed_dependencies'])
                typer.echo(f"{tid} blocked: failed dependencies: {deps}")
            else:
                typer.echo(f"{tid} timeout")
    if result["pending"] or result["blocked"] or any(
            v["state"] != "succeeded" for v in result["finished"].values()):
        raise typer.Exit(1)


# ============================================================== problem log
@log_app.command("append")
@guarded
def log_append(target: str = typer.Argument(..., help="runset ref node/YYYYMMDD-n (or a node)"),
               problem: str = typer.Option(..., "-p", "--problem", help="problem summary (natural language)"),
               solution: str = typer.Option(..., "-s", "--solution", help="solution summary"),
               code: str = typer.Option("", "-c", "--code", help="how the code change solves it"),
               task: Optional[str] = typer.Option(None, "--task"), author: str = typer.Option("human")):
    """Append a time-first entry (问题与解决方案总结 / 代码修改说明) to problem-analysis.log."""
    out(service.problem_log_append(target, problem=problem, solution=solution, code=code, task_id=task,
                                   author=author))


@log_app.command("show")
@guarded
def log_show(target: str):
    try:
        d, _ = service.runset_dir(target)
    except TreeError:
        d = Tree().get(target).dir
    p = d / "problem-analysis.log"
    out(p.read_text() if p.exists() else "(empty)")


# ================================================================== results
@app.command()
@guarded
def result(ref: str, as_json: bool = typer.Option(False, "--json")):
    """Runset summary: tasks, binary, post summary.json metrics and reports."""
    r = service.result_get(ref)
    if as_json:
        out(r)
        return
    typer.echo(f"{r['ref']}  ({'sealed' if r['sealed'] else 'open'})  {r['purpose']}")
    for t in r["tasks"]:
        typer.echo(f"  {t['kind']:5s} {t['state']:9s} {t['counts']}  {t.get('failure') or ''}")
    if r.get("bin"):
        typer.echo(f"  bin {r['bin'].get('emu')}")
    for step, summ in r["post"].items():
        typer.echo(f"  post/{step}:")
        for k, v in list(summ.items())[:25]:
            typer.echo(f"    {k} = {v}")
    for step, md in r["reports"].items():
        typer.echo(f"\n--- post/{step} report ---\n{md[:4000]}")


@app.command()
@guarded
def compare(refs: List[str] = typer.Argument(..., help="baseline names or runset refs; first = reference"),
            points: Optional[str] = typer.Option(None, help="points.json / points file (default: from the "
                                                            "first runset ref)"),
            block: int = 2):
    """Ad-hoc perf-keys comparison into $FEATX_ROOT/compare/<hash>/ (no task needed)."""
    import hashlib

    from .baselines import resolve_refs
    from .post import plugins
    runs = []
    pts = points
    for ref in refs:
        label, target = plugins._labelled(ref)
        root = resolve_refs(target)
        runs.append(f"{label}={root}")
        if not pts and "/" in ref and not ref.startswith("/"):
            cand = Path(root) / "points.json"
            pts = str(cand) if cand.is_file() else None
    if not pts:
        fail("--points is required when no runset ref is given")
    key = hashlib.sha256(json.dumps([runs, pts, block]).encode()).hexdigest()[:12]
    dst = config.compare_dir() / key
    cmd = [sys.executable, str(plugins.SCRIPTS_DIR / "perf_keys.py"), "--runs", *runs, "--points", pts,
           "--block", str(block), "-o", str(dst)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    typer.echo((dst / "report.md").read_text())
    typer.secho(f"== {dst}", fg=typer.colors.BLUE, err=True)


# ============================================================== leaderboard
@app.command("leaderboard")
@guarded
def leaderboard_cmd(metric: Optional[str] = typer.Option(None, "-m", "--metric", help="<post step>.<summary key>"),
                    cls: Optional[str] = typer.Option(None, "--class", help="comparability class key"),
                    node: Optional[str] = typer.Option(None, "--node", help="subtree prefix"),
                    order: Optional[str] = typer.Option(None, help="asc|desc (default by metric name)"),
                    best: bool = typer.Option(False, "--best", help="best runset per node only"),
                    all_: bool = typer.Option(False, "-a", "--all", help="include incomplete runsets"),
                    as_json: bool = typer.Option(False, "--json")):
    """Rank runsets by a post metric inside one comparability class (same reference, PERF
    block, W, I and point set)."""
    r = leaderboard.build(metric=metric, order=order, cls=cls, node=node, best_per_node=best,
                          include_incomplete=all_)
    if as_json:
        out(r)
        return
    if not r["metric"]:
        typer.echo(tr("no post summaries yet"))
        return
    typer.echo(f"metric {r['metric']} ({r['order']})")
    for c in r["classes"]:
        mark = "*" if c["key"] == r["class"] else " "
        typer.echo(f" {mark} {c['label']}  [{c['complete_rows']}/{c['rows']} complete]  {c['key']}")
    typer.echo("")
    for row in r["rows"]:
        rank = f"{row['rank']:>3d}" if row["rank"] else "  -"
        pts = f"{row['common_points']}/{row['points']}" if row["points"] is not None else ""
        flag = "" if row["complete"] else "  INCOMPLETE"
        typer.echo(f"{rank}  {row['value']:>12.4f}  {row['runset']:40s} {pts:>7s}  "
                   f"{row['verdict'] or '':10s} {row['purpose'] or ''}{flag}")
    if not r["rows"]:
        typer.echo(tr("no complete runset in this class (use -a to list incomplete ones)"))
    others = [m["name"] for m in r["metrics"] if m["name"] != r["metric"]]
    if others:
        typer.echo("\nother metrics: " + ", ".join(others))


# ===================================================================== init
@app.command()
@guarded
def init(root: Optional[str] = typer.Option(None, "--root", help="data root (default: the current one, "
                                             "~/.local/share/featx for a new user)"),
         site: Optional[str] = typer.Option(None, "--site", help="site profile: featx/sites/<name>.yaml "
                                             "or a YAML path (default: the one detected on this host, "
                                             "e.g. nfs-share; 'none' for no site)"),
         repo: Optional[str] = typer.Option(None, "--repo", help="your XiangShan checkout (git.repo)"),
         daemon_host: Optional[str] = typer.Option(None, "--daemon-host", help="host featxd runs on"),
         port: Optional[int] = typer.Option(None, "--port", help="featxd port (default 8765 + uid % 200)"),
         adopt: bool = typer.Option(False, "--adopt", help="the root has a featx.yaml: merge into it"),
         set_: List[str] = typer.Option([], "--set", help="more user-layer keys: key.sub=value")):
    """Create (or adopt) a data root: its featx.yaml, the root pointer and the API token."""
    from . import bootstrap
    r = bootstrap.init(root, site, repo, daemon_host, port, adopt, parse_kv(set_))
    typer.echo(f"{tr('adopted' if r['adopted'] else 'created')} {r['root']}  (user layer {r['user_layer']})")
    typer.echo(yaml.safe_dump(r["written"], sort_keys=False).rstrip() if r["written"] else "(no keys changed)")
    if r["site_detected"]:
        typer.echo(f"(site {r['written']['site']} detected on this host; --site none to go without)")
    if r["pointer"]:
        typer.echo(f"root pointer: {r['pointer']}")
    typer.echo("\n" + tr("next:"))
    for line in bootstrap.next_steps():
        typer.echo("  " + line)


@app.command()
@guarded
def doctor(ssh: bool = typer.Option(True, "--ssh/--no-ssh", help="also ssh to every host"),
           as_json: bool = typer.Option(False, "--json")):
    """Check the configured paths, tools and hosts (ok / warn / fail)."""
    from . import doctor as doc
    r = doc.run(ssh=ssh)
    if as_json:
        out(r)
    else:
        colors = {"ok": typer.colors.GREEN, "warn": typer.colors.YELLOW, "fail": typer.colors.RED}
        for c in r["checks"]:
            typer.secho(f"{c['status']:4s}  {c['check']:32s} {c['detail']}", fg=colors[c["status"]])
        typer.echo(f"\n{r['counts']['ok']} ok, {r['counts']['warn']} warn, {r['counts']['fail']} fail  "
                   f"(root {config.root()}, site {config.settings().get('site') or 'none'})")
    if not r["ok"]:
        raise typer.Exit(1)


# =================================================================== config
@config_app.command("show")
@guarded
def config_show(layer: str = typer.Argument("effective", help="effective|user|defaults")):
    v = config.config_view()
    if layer == "effective":
        typer.echo(yaml.safe_dump(v["effective"], sort_keys=False).rstrip())
    elif layer in ("user", "defaults"):
        typer.echo(f"# {v['path']}" if layer == "user" else "# package defaults")
        typer.echo(v[f"{layer}_text"].rstrip())
    else:
        fail("layer must be effective, user or defaults")


def _config_write(text: str) -> None:
    c = via_daemon()
    r = c.put("/config", {"text": text}) if c else config.write_user_layer(text)
    out(r)
    if r.get("restart_needed"):
        typer.secho("restart featxd for: " + ", ".join(r["restart_needed"]), fg=typer.colors.YELLOW)


@config_app.command("set")
@guarded
def config_set(pairs: List[str] = typer.Argument(..., help="key.sub=value ... (value parsed as YAML; "
                                                 "'key=null' removes the override)")):
    """Merge key=value pairs into the user layer ($FEATX_ROOT/featx.yaml)."""
    merged = config.deep_merge(config.config_view()["user"], parse_kv(pairs))
    _config_write(yaml.safe_dump(merged, sort_keys=False))


@config_app.command("edit")
@guarded
def config_edit():
    text = typer.edit(config.config_view()["user_text"] or "# overrides of the featx defaults\n",
                      extension=".yaml")
    if text is None:
        typer.echo("unchanged")
        return
    _config_write(text)


@app.command()
@guarded
def preview(path: str = typer.Argument(..., help="file or dir under the data/worktree roots "
                                       "(relative = from $FEATX_ROOT)"),
            tail: Optional[int] = typer.Option(None, "--tail", help="last N lines"),
            skip_perf: bool = typer.Option(False, help="drop [PERF lines"),
            depth: int = typer.Option(2, help="listing depth for a directory")):
    """Preview a log/result file, or list a directory (what the UI file viewer shows)."""
    if files.resolve(path).is_dir():
        r = files.listing(path, depth=depth)
        typer.echo(r["root"])
        for f in r["files"]:
            typer.echo(f"  {store.human_size(f['size']):>8s}  {f['path']}")
        return
    r = files.read(path, tail=tail, skip_perf=skip_perf)
    typer.echo(r["text"])
    if r["truncated"]:
        typer.secho(f"[truncated: {store.human_size(r['size'])} total]", fg=typer.colors.YELLOW, err=True)


@app.command()
@guarded
def ui():
    """Print the web UI address and the tunnel command."""
    info = daemon_info()
    if not daemon_alive(info):
        fail("featxd not running (featx daemon start)", 3)
    host, port = info["host"], info["port"]
    typer.echo(f"on the cluster:   http://{host}:{port}/ui/")
    typer.echo(f"from a laptop:    ssh -N -L {port}:{host}:{port} <login-node>   then  http://localhost:{port}/ui/")
    typer.echo(f"token:            cat {config.token_path()}")


# ================================================================== storage
@app.command()
@guarded
def storage(worktrees: bool = typer.Option(False, help="also size worktrees (slow)"),
            as_json: bool = typer.Option(False, "--json")):
    """Disk usage per node, bincache (with references), trash, compare cache."""
    r = service.storage_report(worktrees=worktrees)
    if as_json:
        out(r)
        return
    typer.echo(f"featx data root {r['root']}: {r['total_human']}")
    for n in r["nodes"]:
        wt = f"  worktree {n['worktree_human']}" if "worktree_human" in n else ""
        typer.echo(f"  {n['human']:>8s}  {n['path']}  ({n['runsets']} runsets){wt}")
    for b in r["bincache"]:
        refs = f"{len(b['refs'])} refs" if b["refs"] else "UNREFERENCED"
        typer.echo(f"  {b['human']:>8s}  bincache/{b['key']}  {b.get('label') or ''}  {refs}")
    for t in r["trash"]:
        typer.echo(f"  {t['human']:>8s}  {t['path']}")
    typer.echo(f"  {store.human_size(r['collect_cache_bytes']):>8s}  compare/collect-cache")


@app.command()
@guarded
def gc(apply: bool = typer.Option(False, "--apply", help="actually delete"),
       build_dirs: bool = typer.Option(False, help="include worktree build/ of done/abandoned nodes")):
    """Reference-aware cleanup (dry run unless --apply)."""
    r = service.gc(apply=apply, build_dirs=build_dirs)
    for cnd in r["candidates"]:
        typer.echo(f"  {cnd['human']:>8s}  {cnd['kind']:14s} {cnd['path']}")
    typer.echo(f"{'deleted' if apply else 'would delete'} {r['total_human']} ({len(r['candidates'])} items)"
               + ("" if apply else "; rerun with --apply"))


# ================================================================ baselines
@base_app.command("list")
@guarded
def baseline_list():
    for name, b in service.baselines_list().items():
        typer.echo(f"{name:16s} {b['root']}\n{'':16s} {b.get('description', '')}")


@base_app.command("add")
@guarded
def baseline_add(name: str = typer.Argument(..., help="name used as @baseline(NAME) / in a baselines: list"),
                 root: str = typer.Argument(..., help="result root: a dir of <bench>_<cp>_<weight> point dirs"),
                 description: str = typer.Option("", "-m", help="one-line description (campaign, W/I, coverage)"),
                 binary: Optional[str] = typer.Option(None, help="the emu that produced it "
                                                                 "(provenance; gc keeps that bincache entry)"),
                 collected: Optional[str] = typer.Option(None, help="its collect output dir "
                                                                    "(@baseline(NAME).collected, used by pair/diagnose)")):
    """Add or replace a named baseline (see `featx help baselines`)."""
    out(service.baseline_add(name, root, description, binary=binary, collected=collected))


@base_app.command("rm")
@guarded
def baseline_rm(name: str, force: bool = typer.Option(False, "-f", "--force",
                                                      help="remove even while workloads / post recipes name it")):
    """Remove a named baseline (refused while a workload or a post recipe uses it)."""
    out(service.baseline_rm(name, force=force))


# ================================================================ checkpoints
def _ckpt_table(rows: list[dict]) -> None:
    if not rows:
        typer.echo("no checkpoint sets (checkpoints.roots: "
                   + (", ".join(map(str, (config.settings().get("checkpoints") or {}).get("roots") or [])) or "unset") + ")")
        return
    w = max(len(r["name"]) for r in rows)              # full names: run.checkpoint takes them
    typer.echo(f"{'set':{w}s} {'layout':8s} {'points':>6s} {'bench':>5s} insts restorer  traced_by / problem")
    for r in sorted(rows, key=lambda r: (r.get("spec") or "", r["name"])):
        note = "; ".join(x for x in ("multi-hart (not runnable)" if r["multihart"] else "", r["error"]) if x) \
            or ", ".join(r["traced_by"])
        typer.echo(f"{r['name']:{w}s} {r['layout'] or '-':8s} {r['n_points']:6d} {r['benches']:5d} "
                   f"{'yes' if r['has_insts'] else 'no':5s} {'yes' if r['restorer_needed'] else 'no':8s}  {note}")


@ckpt_app.command("list")
@guarded
def ckpt_list(spec: Optional[str] = typer.Option(None, help="SPEC version: 06, 17, 26"),
              all_sets: bool = typer.Option(False, "--all", help="include multi-hart and broken sets"),
              as_json: bool = typer.Option(False, "--json")):
    """Checkpoint sets under checkpoints.roots (use a name as run.checkpoint with run.mode=exec)."""
    rows = service.checkpoints(spec=spec, all_sets=all_sets)
    out(rows) if as_json else _ckpt_table(rows)


@ckpt_app.command("show")
@guarded
def ckpt_show(name: str = typer.Argument(..., help="set name (featx ckpt list) or set directory"),
              bench: Optional[str] = typer.Option(None, help="only this benchmark (prefix match)"),
              as_json: bool = typer.Option(False, "--json")):
    """One set: layout, weights json, and its points with weights and image files."""
    r = service.checkpoint_show(name, bench=bench)
    if as_json:
        return out(r)
    for k in ("name", "path", "layout", "json", "json_sha256", "multihart", "restorer_needed", "traced_by",
              "missing", "extra", "error"):
        typer.echo(f"{k:16s} {r[k]}")
    typer.echo(f"{'points':16s} {len(r['points'])} in {len({p['bench'] for p in r['points'].values()})} benchmarks")
    for n, p in r["points"].items():
        typer.echo(f"  {n:48s} {p['weight']:>12s}  {p['image']}")


@ckpt_app.command("rescan")
@guarded
def ckpt_rescan(spec: Optional[str] = typer.Option(None, help="only sets of this SPEC version")):
    """Rebuild the cached set indexes (they also rebuild by themselves when a set changes)."""
    _ckpt_table(service.checkpoints(spec=spec, refresh=True))


@bin_app.command("import")
@guarded
def bin_import(path: str, label: str = typer.Option("", "-m", help="free-text label"),
               mode: Optional[str] = typer.Option(None, help="trace|exec: the binary's TRACERTL_MODE; "
                                                             "a run with --bin <key> follows it")):
    """Register an existing emu binary in the bincache (use the key with submit --bin)."""
    out(service.bin_import(path, label, mode))


# ================================================================== plugins
@plugin_app.command("list")
@guarded
def plugin_list():
    """Installed post plugins (featx/post/manifests and $FEATX_ROOT/plugins)."""
    from .post import plugins
    for name, man in sorted(plugins.available().items()):
        desc = " ".join(str(man.get("description") or "").split())
        missing = plugins.missing_requirements(man)
        off = f"[off: set {', '.join(missing)}] " if missing else ""
        typer.echo(f"{name:14s} {off}{desc[:100]}{'...' if len(desc) > 100 else ''}")


@plugin_app.command("show")
@guarded
def plugin_show(name: str, raw: bool = typer.Option(False, "--raw", help="print the manifest YAML")):
    """A plugin's parameters, dependencies and outputs (or its manifest with --raw)."""
    from .post import plugins
    man = plugins.get(name)
    if raw:
        out(Path(man["_path"]).read_text())
    else:
        out(docs.plugin_markdown({name: man}, level=2))


# ===================================================================== help
@app.command("help")
@guarded
def help_cmd(topic: Optional[str] = typer.Argument(None, help="topic, plugin name, or 'all'")):
    """Reference docs (docs/reference.md): the topic list, or one topic."""
    if not topic:
        typer.echo("featx help <topic>   (docs/reference.md; also MCP `docs` and the UI Docs page)\n")
        for t in docs.index()["topics"]:
            typer.echo(f"  {t['topic']:12s} {t['title']}")
        typer.echo("  <plugin>     one post plugin (featx plugin list)\n  all          everything")
        return
    out(docs.topic(topic))


# =================================================================== daemon
def _runwatch() -> list[str]:
    return [str(config.BIN_DIR / "runwatch.sh")]


@daemon_app.command("start")
@guarded
def daemon_start(foreground: bool = typer.Option(False, "--fg")):
    """Start featxd on the configured daemon host under runwatch (ssh if needed)."""
    info = daemon_info()
    if daemon_alive(info):
        typer.echo(f"featxd already running on {info['host']}:{info['port']} (pid {info['pid']})")
        return
    exe = Path(sys.executable).parent / "featxd"
    if foreground:
        os.execv(str(exe), [str(exe)])
    host = config.settings()["daemon"].get("host") or "localhost"
    rw_dir = config.root() / ".runwatch"
    env = f"FEATX_ROOT={shlex.quote(str(config.root()))} RUNWATCH_DIR={shlex.quote(str(rw_dir))}"
    if os.environ.get("FEATX_WT_ROOT"):
        env += f" FEATX_WT_ROOT={shlex.quote(os.environ['FEATX_WT_ROOT'])}"
    inner = f"{env} {shlex.quote(_runwatch()[0])} start featxd -- {shlex.quote(str(exe))} </dev/null >/dev/null 2>&1"
    from .sched.placement import is_local
    cmd = ["bash", "-c", inner] if is_local(host) else \
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, inner]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    for _ in range(40):
        time.sleep(0.5)
        info = daemon_info()
        if daemon_alive(info, 30) and info.get("heartbeat", 0) > time.time() - 10:
            typer.echo(f"featxd up on {info['host']}:{info['port']} (pid {info['pid']}); "
                       f"log {rw_dir}/featxd/log")
            return
    fail(f"featxd did not come up; see {rw_dir}/featxd/log on {host}")


@daemon_app.command("stop")
@guarded
def daemon_stop():
    info = daemon_info()
    if not daemon_alive(info):
        typer.echo("featxd not running")
        return
    from .sched.executor import alive_on_host, kill
    kill(info["host"], info["pid"], "TERM")
    for _ in range(30):
        time.sleep(1)
        if alive_on_host(info["host"], info["pid"]) is False:
            break
    else:
        fail(f"featxd pid {info['pid']} on {info['host']} still alive 30 s after TERM")
    # The daemon marks itself stopped on a clean exit; cover a hard death too, otherwise
    # a start right after stop would trust the still-fresh heartbeat.
    cur = daemon_info()
    if cur.get("pid") == info["pid"] and not cur.get("stopped"):
        cur["stopped"] = time.time()
        store.write_json(config.daemon_json(), cur)
    typer.echo(f"featxd pid {info['pid']} on {info['host']} stopped (running jobs keep going)")


@daemon_app.command("status")
@guarded
def daemon_status():
    info = daemon_info()
    if not daemon_alive(info):
        fail(f"featxd not running (daemon.json: {info or 'missing'})", 3)
    st = client().get("/status")
    out({"host": info["host"], "port": info["port"], "pid": info["pid"],
         "heartbeat_age_s": round(time.time() - info["heartbeat"], 1), **st})


@daemon_app.command("hosts")
@guarded
def daemon_hosts(refresh: bool = False):
    """Placement view: free cores/mem and featx jobs per host."""
    for h in client().get("/hosts", refresh=refresh, timeout=120):
        if h.get("state") == "unprobed":
            typer.echo(f"{h['host']:8s} unprobed")
            continue
        typer.echo(f"{h['host']:8s} free {h.get('free_cores', 0):6.1f} cores {h.get('free_mem_gb', 0):7.1f} GB "
                   f" featx jobs {h.get('featx_jobs', 0):3d}  {h.get('state', '')}")


@daemon_app.command("watchdog")
@guarded
def daemon_watchdog():
    """For crontab (*/5): restart featxd if its heartbeat is stale."""
    if not daemon_alive(daemon_info(), 120):
        daemon_start(False)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
