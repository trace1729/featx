# featx — feature exploration & task scheduler

Organizes experiments as a feature tree (`category/feature/subfeature`). The tree decides where
worktrees, builds, runs, logs and results live. A single daemon (`featxd`) schedules
build/run/post tasks across the cluster. An MCP server gives an LLM the same functions as the CLI:
read context, submit, wait for failures, log the analysis, relaunch.

- Code: `toolsets/featx` (Python 3.12, uv venv in `.venv`)
- Data: `/nfs/share/gkc/featx` (`$FEATX_ROOT`)
- Worktrees: `/nfs/home/gongkaichen/tracertl/featx-wt/<category>/<feature>/<sub>` (branch `<path>`, e.g. `wpe/overlay/smoke`; `git.branch_prefix` adds a prefix)

## Install

```bash
cd toolsets/featx
uv venv .venv && uv pip install --python .venv/bin/python -e . \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple
export PATH=$PWD/.venv/bin:$PATH          # featx, featxd, featx-mcp
featx daemon start                         # on node024 under runwatch (ssh if needed)
claude mcp add featx -- $PWD/.venv/bin/featx-mcp
```

Optional watchdog (crontab):
`*/5 * * * * /nfs/home/gongkaichen/tracertl/toolsets/featx/.venv/bin/featx daemon watchdog`.
Jobs keep running while the daemon is down. On restart it re-adopts them from NFS state.
`daemon.json` is refreshed every 5 s by its own thread. That thread stops only if the scheduler
loop makes no progress for 10 min, so the watchdog still restarts a hung daemon. Launches use ssh
without holding the scheduler lock, at most `placement.max_launches_per_tick` (128) per tick. A
large panel therefore no longer blocks submit/cancel or the API.
Kills (cancel, dead attempts) and liveness probes of stale heartbeats are batched into one ssh
per host and run without the lock as well. `placement.max_total_cores` (300) caps remote
run cores; `placement.max_build_cores` (64) gives remote builds an independent budget.
Jobs beyond either budget stay queued, oldest task first within that budget, until cores
are released. Both budgets still require available host CPU and memory. A job larger than
its budget can run alone in that budget; 0 disables that budget's cap. `featx daemon status`
shows total `cores_in_use` plus `run_cores_in_use` and `build_cores_in_use`.

## Layout

```
$FEATX_ROOT/
  featx.yaml                 overrides of featx/defaults/featx.yaml (hosts, liveness, retry ...)
  daemon.json                daemon host/port/pid/heartbeat (CLI + MCP discover it here)
  baselines.yaml             named result roots (exec-0903, overlay-0928, ...)
  bincache/<key>/emu         built or imported binaries (+ manifest.json)
  events/inbox.jsonl         failure / retry / task_done / runset_done ... events
  tasks/<id>.json            task state (written only by the daemon)
  tree/<cat>/<feat>/<sub>/
    node.yaml                id, hypothesis, verdict, evidence, git, moves
    recipes/{build,run,post}.yaml   own layer; inherited + deep-merged from ancestors
    scripts/                 helper scripts (${scripts}/name in recipes)
    <YYYYMMDD-n>/            runset: experiment.yaml (provenance, recipe snapshot, bin),
                             problem-analysis.log, bin/emu, scripts/, build/, run/<point>/, post/<step>/
```

Runsets are sealed once all their tasks succeed. A rerun after a fix is a new derived runset
(`task resubmit`). `node mv` moves dirs, worktrees and branches as a cascade, and leaves a forwarding
symlink so old absolute paths keep resolving.

## Typical loop

```bash
featx node add wpe/real/after -p -H "after-anchor pick lowers astar error" --worktree
featx recipe set wpe/real run W=20000000 I=20000000 env.TRACERTL_WP_PICK=after workload=spec06
featx recipe set wpe/real post -f post.yaml      # steps: {pk: {plugin: perf-keys, baselines: [overlay-0928]}}
featx submit wpe/real/after                      # build -> run -> post, new runset
featx tree                                       # status/ETA per node
featx ps ; featx eta ; featx logs <task> -j <point> -f
featx events --wait 600                          # failure / retry / done
featx log append wpe/real/after/20261012-1 -p "<problem>" -s "<fix>" -c "<code explanation>"
featx task relaunch <task>                       # design failures need the log entry above
featx result wpe/real/after/20261012-1
featx node set wpe/real/after --verdict supported --evidence wpe/real/after/20261012-1
```

`--bin <file | bincache key | runset>` runs an existing binary without a build. Use
`featx bin import <emu> -m label --mode trace|exec` to register one. `--point` picks points and
`--set run.I=300000` overrides a recipe key.

## Reference and presets

`docs/reference.md` documents recipe keys, variables, point selection, reference forms and every
post plugin's parameters, outputs and dependencies. The same text is available as:

- `featx help` (topic list) and `featx help <topic|plugin|all>` on the CLI;
- the `docs` MCP tool;
- the **Docs** page of the web UI.

Its plugin section is generated from the manifests: run `python -m featx.docs` after editing
one (a test fails when it is stale).

The common knobs are a few recipe keys (`featx help build`, `featx help run`):

```bash
featx submit N build --set build.tool=verilator              # gsim (default) | verilator | script
featx submit N build run --set build.mode=exec               # trace (default) | exec; the run follows the build
featx submit N build --set build.options.chiseldb=true       # also chiselmap, constantin, threads, flags ...
featx recipe set N run workload=spec17 'benchmarks=[mcf]'  # spec06 | spec17 | spec26 (all points by default)
featx submit N run --bin <emu> --point mcf_165900000000_0.031476 --set run.W=100000 --set run.I=200000
```

Other commands: `featx plugin list|show NAME`, `featx baseline add|rm|list`,
`featx worktree rename-branch NODE` (e.g. an old `featx/<path>` branch to `<path>`).

## Web UI (leaderboard)

featxd serves a React app at `/ui/` from the committed bundle `featx/web/dist`. Nodes need no
node runtime. `featx ui` prints the URL, the tunnel command and the token location.

```bash
ssh -N -L 8765:node024:8765 <login-node>       # or a VS Code port forward of node024:8765
# open http://localhost:8765/ui/ and paste the token from: cat ~/.config/featx/token
```

Pages:

- **Leaderboard**: rank runsets by any post metric (`<step>.<summary key>`, default
  `pk.self.mean_abs_err_pct`). Runsets are only ranked within a comparability class: same
  reference, PERF block, `W`, `I` and point set. Runsets that did not finish every point are shown
  only with "show incomplete" and are never ranked.
- **Node**: hypothesis, verdict, evidence, recipes (own-layer editor and explain view), scripts,
  worktree, runsets, submit form.
- **Runset**: stage chips, per-point progress and ETA, live log tail, post report, file browser,
  problem log, cancel/relaunch/resubmit/unblock.
- **Tasks**, **Events** (filter + ack), **Storage** (usage, gc dry run, then apply), **Settings**
  (`featx.yaml` editor, baselines, bin import), **Docs** (`docs/reference.md` by topic).
- In the left tree, drag a node onto another to move it. The dry-run plan is shown before you confirm.

Everything the UI does also has a CLI form:

```bash
featx leaderboard [--metric pk.self.ipc_geomean] [--class KEY] [--node wpe/] [--best] [--all]
featx config show [user|defaults|effective] ; featx config set retry.max_infra_attempts=4 ; featx config edit
featx node mv SRC DST [--parents] --dry-run
featx preview <path under $FEATX_ROOT or the worktree root> [--tail N]
```

The matching MCP tools are `leaderboard` and `config_get`.

Frontend development (Node ≥ 18):

```bash
cd featx/web && npm ci
npm run dev        # Vite on :5173, proxies the API to $FEATX_DEV_API (default a dev featxd on 127.0.0.1:8766)
npm test           # vitest
npm run build      # tsc + vite -> featx/web/dist (commit it; a running featxd picks it up)
```

## Failure handling

Every failed job is classified by `config/classify.yaml`.

- **infra** (ssh 255, host down, OOM kill, stale heartbeat): retried automatically on another
  host, at most `retry.max_infra_attempts` times. The scheduler kills the old attempt's leftover
  processes (the whole session, including the emu under `timeout`). Each retry emits a `retry` event.
- **design** (`TraceRTL Error at inst`, `Stuck`, `ABORT`, build `error:` ...): emits a `failure` event
  with the signature and the last 80 log lines, plus `failure.json` in the job dir.
  - `task relaunch` is refused until `problem-analysis.log` has an entry newer than the failure.
  - Relaunches are limited by `retry.max_design_relaunches`. After that, a human must run
    `featx task unblock`.

## Post-processing plugins

Manifests live in `featx/post/manifests/*.yaml`, or in `$FEATX_ROOT/plugins/` to extend without a
code change. Each has a `cmd` template and default parameters (`with`), and must write
`summary.json`. Built-in plugins:

- `perf-keys`: formal-window IPC/counters vs baselines.
- `collect`, `pair`, `diagnose`, `branch-mpki`: wrap `microarch/spec_trace_compare/03_analysis`.

`featx compare <runset|baseline>...` runs `perf-keys` ad hoc.

A step can also run its own command instead of a plugin, for example to call a node script:

```yaml
steps:
  stats: {cmd: "bash ${scripts}/stats.sh ${run} ${out}", after: [pk]}   # must leave ${out}/summary.json
```

## Helper scripts

Scripts saved on a node (UI scripts tab, `featx script write`, MCP `script_write`) live in
`<node>/scripts/`. They are inherited like recipes: on submit, featx copies `$FEATX_ROOT/scripts`,
then each ancestor's scripts, then the node's own into `<runset>/scripts/` (a later layer wins
on a name clash). `${scripts}` in build, run and post recipes is that copy. So:

- an inherited recipe still finds its parent's script;
- the runset keeps the exact scripts it ran;
- a relaunch refreshes the copy, so a script fix is picked up like a worktree fix.

A build that uses `${scripts}` is cached by the scripts' content, so an unchanged rebuild still
hits the cache. Built-in plugin helpers are `${plugin.scripts}`.

## MCP tools

| Group | Tools |
|---|---|
| Tree | `tree_list`, `node_context`, `node_create`, `node_update`, `node_move` |
| Recipes & scripts | `recipe_get`, `recipe_set`, `plugins_list`, `docs`, `script_write`, `script_read`, `worktree_create` |
| Tasks | `task_submit`, `task_status`, `task_log`, `task_relaunch`, `task_resubmit`, `task_cancel` |
| Events | `events_wait` (blocking ≤ 30 min), `events_ack` |
| Results | `problem_log_append`, `result_get`, `runsets_list`, `baselines`, `storage_report`, `hosts` |

Expected errors (unknown node, refused relaunch, daemon down) come back to the model as tool
errors with their message.

## Storage

`featx storage [--worktrees]` reports usage per node and for bincache.

`featx gc [--build-dirs]` lists unreferenced bincache entries, trash and build dirs. It never
touches anything referenced by an `experiment.yaml` or an active task. Pass `--apply` to delete.

`featx node rm <node> -f` moves the subtree into `tree/.trash`. Add `--drop-worktrees` to also
delete its worktrees (uncommitted diffs saved under `.trash/worktree-patches`), the featx branches
that carry no commits beyond their base, old forwarding symlinks and empty dirs. Branches with their
own commits are kept and reported unless you also pass `--drop-branches`.

## Tests

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider    # tmp data root; fake emu via real featx-exec
```

Fixes found while building and verifying featx are recorded in `problem-analysis.log`.
