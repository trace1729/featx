"""featx MCP server (stdio). Tree/recipe/script/worktree/result tools work on the data root
directly; task and event tools go through featxd.

Register:  claude mcp add featx -- /nfs/home/gongkaichen/tracertl/toolsets/featx/.venv/bin/featx-mcp
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any

import anyio
import yaml
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import config, leaderboard as board, service
from .client import ApiError, Client, DaemonDown
from .post import plugins
from .sched.scheduler import SchedError
from .tree import TreeError

INSTRUCTIONS = """\
featx organizes experiments as a feature tree (category/feature/subfeature). Each node may own a
git worktree, recipes (build/run/post YAML, inherited and deep-merged from ancestors) and helper
scripts. Every submission creates a dated runset <node>/<YYYYMMDD-n> holding build/, run/<point>/,
post/<step>/ and problem-analysis.log.

Typical loop:
 1. tree_list -> node_context(node): hypothesis chain, effective recipes, worktree state, latest
    runsets and their post summaries.
 2. Implement in the node's worktree (path in node_context.git), adjust recipes with recipe_set,
    add helper scripts with script_write.
 3. task_submit(node, kinds=[build,run,post]) -> task ids; then events_wait.
 4. On a `failure` event: task_log for details, fix, then problem_log_append(runset, problem,
    solution, code) BEFORE task_relaunch (design failures are refused without a newer entry and are
    limited by a relaunch budget). Use task_resubmit when recipes/binaries changed (new runset).
 5. result_get(runset) to read metrics; node_update(verdict=..., add_evidence=[runset]).
Infra failures (ssh, host down, OOM) are retried automatically on another host and only appear as
`retry` events. Always ack events you have handled (events_ack).
"""

server = MCPServer(name="featx", instructions=INSTRUCTIONS)

# Expected failures (unknown node, refused relaunch, daemon down...) must reach the model with
# their message; MCPServer hides the text of any other exception as an internal crash.
USER_ERRORS = (TreeError, SchedError, ApiError, DaemonDown, KeyError, ValueError, FileNotFoundError,
               TimeoutError, PermissionError)


def tool(fn):
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except USER_ERRORS as error:
                raise ToolError(f"{type(error).__name__}: {error}") from error
    else:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except USER_ERRORS as error:
                raise ToolError(f"{type(error).__name__}: {error}") from error
    server.tool()(wrapper)
    return fn


def _c(timeout: float = 120) -> Client:
    return Client(timeout=timeout)


# ------------------------------------------------------------------ tree
@tool
def tree_list(root: str | None = None) -> str:
    """Feature tree as text: verdict marks (✔ supported, ✘ refuted, ? inconclusive), latest runset
    task states and ETA per node. `root` limits to a subtree."""
    from .client import daemon_alive
    from .tree import Tree
    if daemon_alive():
        return _c().get("/tree", fmt="text", root=root)
    return service.tree_text(Tree(), None, root) or "(empty tree)"


@tool
def node_context(node: str, max_runsets: int = 3) -> dict:
    """Everything needed to work on a node: its hypothesis/verdict and the ancestors' chain,
    children, effective and own recipes, scripts, worktree status, latest runset summaries
    (task states, post metrics, problem-log tail)."""
    return service.node_context(node, max_runsets)


@tool
def node_create(path: str, hypothesis: str = "", tags: list[str] | None = None, parents: bool = True,
                worktree: bool = False, base: str | None = None) -> dict:
    """Create a node at category/feature/subfeature (parents=True creates missing ancestors).
    worktree=True also creates its git worktree (branch featx/<path>) from `base`."""
    return service.node_add(path, hypothesis=hypothesis, tags=tags, parents=parents, worktree_create=worktree,
                            base=base)


@tool
def node_update(node: str, hypothesis: str | None = None, verdict: str | None = None,
                verdict_note: str | None = None, status: str | None = None,
                add_evidence: list[str] | None = None, add_tags: list[str] | None = None,
                notes: str | None = None) -> dict:
    """Update a node. verdict: open|supported|refuted|inconclusive (back it with add_evidence =
    runset refs). status: active|parked|done|abandoned."""
    return service.node_update(node, hypothesis=hypothesis, verdict=verdict, verdict_note=verdict_note,
                               status=status, add_evidence=add_evidence, add_tags=add_tags, notes=notes)


@tool
def node_move(src: str, dst: str, parents: bool = True) -> dict:
    """Cascade-move a node subtree (dirs, worktrees, branches; old path keeps a forwarding
    symlink). Refused while tasks under it are active."""
    return service.node_mv(src, dst, parents=parents)


# --------------------------------------------------------------- recipes
@tool
def recipe_get(node: str, kind: str, effective: bool = True, explain: bool = False) -> Any:
    """Recipe `kind` (build|run|post) of a node: effective (after inheritance), own layer only
    (effective=False), or an annotated explanation of which layer set each key (explain=True)."""
    return service.recipe_get(node, kind, effective=effective, explain=explain)


@tool
def recipe_set(node: str, kind: str, yaml_text: str, merge: bool = True) -> dict:
    """Write the node's own recipe layer from YAML. merge=True deep-merges into the existing layer,
    False replaces it. Variables: ${worktree} ${node} ${runset_dir} ${point} ${W} ${I} ...;
    post steps reference plugins (see plugins_list)."""
    data = yaml.safe_load(yaml_text) or {}
    return service.recipe_set(node, kind, data, merge=merge)


@tool
def plugins_list() -> dict:
    """Post-processing plugins with their manifest (cmd template, default params, outputs).
    Add new ones as YAML under $FEATX_ROOT/plugins/."""
    return plugins.available()


@tool
def script_write(node: str, name: str, content: str, executable: bool = True) -> dict:
    """Store a helper script under <node>/scripts/ (usable in recipes as ${scripts}/<name>)."""
    return service.script_write(node, name, content, executable)


@tool
def script_read(node: str, name: str) -> str:
    """Read a node helper script."""
    return service.script_read(node, name)


@tool
def worktree_create(node: str, base: str | None = None, adopt_path: str | None = None) -> dict:
    """Create the node's git worktree (or adopt an existing one at adopt_path)."""
    if adopt_path:
        return service.worktree_create(node, path=adopt_path, adopt=True, base=base)
    return service.worktree_create(node, base=base)


# ----------------------------------------------------------------- tasks
@tool
def task_submit(node: str, kinds: list[str] | None = None, purpose: str = "",
                overrides: dict | None = None, bin: str | None = None, points: list[str] | None = None,
                post_steps: list[str] | None = None, runset: str | None = None) -> dict:
    """Submit build/run/post (default all, chained) as a new runset of `node`.
    overrides: {"run": {"W": 1000000, "I": 2000000}, ...} merged over the effective recipes.
    bin: emu path | bincache key | runset ref to skip the build. points: subset of run points."""
    return _c(300).post("/tasks", {"node": node, "kinds": kinds or ["build", "run", "post"], "purpose": purpose,
                                   "overrides": overrides, "bin": bin, "points": points,
                                   "post_steps": post_steps, "runset": runset}, timeout=300)


@tool
def task_status(task: str | None = None, node: str | None = None, active_only: bool = True) -> Any:
    """One task (with per-job states, attempts, hosts, progress, ETA) or the task list filtered by
    node prefix."""
    if task:
        return _c().get(f"/tasks/{task}")
    return _c().get("/tasks", active=active_only, node=node, limit=100)


@tool
def task_log(task: str, job: str | None = None, stream: str = "out", lines: int = 120) -> dict:
    """Tail of a job log (stream out|err; for run points out=simulator_out.txt,
    err=simulator_err.txt). Default job: first failed, else first running."""
    return _c().get(f"/tasks/{task}/log", job=job, stream=stream, lines=lines)


@tool
def task_relaunch(task: str, jobs: list[str] | None = None, reason: str = "") -> dict:
    """Requeue failed jobs of a task in place (same runset). Design failures require a
    problem_log_append newer than the failure and are budget-limited."""
    return _c().post(f"/tasks/{task}/relaunch", {"jobs": jobs, "reason": reason})


@tool
def task_resubmit(task: str, only_failed: bool = True, overrides: dict | None = None, purpose: str = "") -> dict:
    """New runset derived from the task's runset with re-resolved recipes (after a recipe/code
    fix). only_failed restricts run to the failed points."""
    return _c(300).post(f"/tasks/{task}/resubmit", {"only_failed": only_failed, "overrides": overrides,
                                                    "purpose": purpose}, timeout=300)


@tool
def task_cancel(task: str, jobs: list[str] | None = None) -> dict:
    """Cancel a task (or some of its jobs); running processes are killed."""
    return _c().post(f"/tasks/{task}/cancel", {"jobs": jobs})


# ---------------------------------------------------------------- events
@tool
async def events_wait(timeout: float = 300, types: list[str] | None = None, node: str | None = None,
                      since: int = 0) -> list:
    """Block up to `timeout` s for unacked events (failure, retry, task_done, runset_done,
    budget_exhausted, relaunched...). Returns immediately if some are pending."""
    timeout = max(0.0, min(float(timeout), 1800.0))

    def call():
        return _c(timeout + 30).get("/events", since=since, unacked=True, types=",".join(types) if types else None,
                                    node=node, wait=timeout or None, timeout=timeout + 30)
    return await anyio.to_thread.run_sync(call)


@tool
def events_ack(seqs: list[int] | None = None, upto: int | None = None) -> dict:
    """Acknowledge handled events by seq (or everything up to `upto`)."""
    return _c().post("/events/ack", {"seqs": seqs, "upto": upto})


# --------------------------------------------------------------- results
@tool
def problem_log_append(target: str, problem: str, solution: str, code: str = "", task: str | None = None) -> dict:
    """Append to <runset>/problem-analysis.log (target = node/YYYYMMDD-n, or a node). Format is
    enforced: timestamp, then natural-language problem + solution summary, then how the code change
    solves it (`code`)."""
    return service.problem_log_append(target, problem=problem, solution=solution, code=code, task_id=task,
                                      author="llm")


@tool
def result_get(runset: str) -> dict:
    """Runset result: task states, binary, provenance, post summary metrics and reports."""
    return service.result_get(runset)


@tool
def runsets_list(node: str) -> list:
    """All runsets of a node with task states and post summaries."""
    return service.runsets(node)


@tool
def baselines() -> dict:
    """Named baselines usable in post steps as @baseline(name) or in a `baselines:` list."""
    return service.baselines_list()


@tool
def storage_report() -> dict:
    """Disk usage per node, bincache entries with references, trash, compare cache."""
    return service.storage_report()


@tool
def hosts() -> list:
    """Cluster placement view (free cores/memory, featx jobs per host)."""
    return _c().get("/hosts")


@tool
def leaderboard(metric: str | None = None, cls: str | None = None, node: str | None = None,
                best_per_node: bool = False, include_incomplete: bool = False) -> dict:
    """Rank runsets by a post metric ("<step>.<summary key>", default IPC mean|err|). Only
    runsets in one comparability class (same reference, PERF block, W, I, point set) are ranked
    together; `classes` lists the others, `metrics` the available metric names."""
    return board.build(metric=metric, cls=cls, node=node, best_per_node=best_per_node,
                       include_incomplete=include_incomplete)


@tool
def config_get() -> dict:
    """featx settings: package defaults, the user layer ($FEATX_ROOT/featx.yaml) and the
    effective merge."""
    v = config.config_view()
    return {"path": v["path"], "user": v["user"], "effective": v["effective"],
            "restart_keys": v["restart_keys"]}


def main() -> None:
    config.ensure_layout()
    logging.getLogger("httpx").setLevel(logging.WARNING)   # one INFO line per REST call otherwise
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
