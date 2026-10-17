# featx reference

Every `## <topic> — <title>` section below is a help topic. Read one with
`featx help <topic>` (CLI), the `docs` MCP tool, or the **Docs** page of the web UI.
`featx help` lists the topics. `featx help <plugin>` shows one post plugin.

## concepts — Nodes, runsets, tasks

- **Node**: a directory in the feature tree, `tree/<category>/<feature>/<sub>/`. It holds
  `node.yaml` (hypothesis, verdict, evidence, comment, notes, git), its own recipe layers `recipes/{build,run,post}.yaml`,
  helper `scripts/`, and its runsets.
- **Runset**: one experiment of a node, `<node>/<YYYYMMDD-n>/`. Every `featx submit` creates a new
  one, unless you pass `--runset` to reuse an unsealed runset. It contains:
  - `experiment.yaml`: provenance (commit, diff hash, submodules), the resolved recipes, `bin`;
  - `bin/emu`: the binary the run used (build output, or the `--bin` binary);
  - `build/`, `run/<point>/` (one dir per point: `simulator_out.txt`, `simulator_err.txt`, `env.txt`),
    `post/<step>/` (each step's `summary.json` and reports);
  - `scripts/`: the snapshot of the inherited helper scripts;
  - `problem-analysis.log`: the analysis entries (`featx log append`).
  A runset is sealed once all its tasks succeed. A rerun after a fix is a new derived runset
  (`featx task resubmit`).
- **Task**: one kind (build, run or post) of a runset, made of jobs. A build is one job, a run
  has one job per point, a post has one job per step. The tasks of a submit are chained:
  build → run → post.
- **Node fields** (`featx node update`, MCP `node_update`, the UI header): `hypothesis`,
  `verdict` (open|supported|refuted|inconclusive), `verdict_note`, `status`
  (active|parked|done|abandoned), `evidence` (a list: `--evidence` adds, `--rm-evidence` removes;
  the API also accepts the full list), `tags`, `notes` (Markdown, the Notes tab) and `comment`
  (free-form thoughts). Other keys (git, moves, created) cannot be changed this way.
- **Code changes**: `node_context.git` reports `ahead` and `files_changed` against the base; the
  UI worktree tab and `GET /worktrees/changes?ref=NODE` list the commits and files of the main and
  submodule worktrees, `GET /worktrees/diff?ref=NODE&sub=&path=` one file's diff.
- **Ref forms**: a node is its path (`wpe/overlay/smoke`) or its id. A runset is
  `<node path>/<YYYYMMDD-n>` or a runset id prefix (≥ 6 characters).

## launch — Launching a run directly

```bash
featx node add t/smoke -p -H "smoke test"           # -p creates the missing parents
featx submit t/smoke run --bin <emu> --point astar_biglakes_38_0.017849 --set run.W=100000 --set run.I=200000
featx ps ; featx logs <task> -j astar_biglakes_38_0.017849 -f
```

- `featx submit NODE [build] [run] [post]`: the default is all three. Without `build`, `run`
  needs `--bin`.
- `--bin` takes an emu file, a bincache key (`featx bin import <emu> -m label --mode exec`), or a
  runset ref, which reuses that runset's built binary.
- `--point NAME` (repeatable) replaces the recipe's `points`/`points_file`/`suite` for this
  submit (see `featx help points`).
- `--set kind.key=value` overrides one key for this submit only. The value is parsed as YAML:
  `--set run.points=[a_1_0.1,b_2_0.2]`, `--set build.options.chiseldb=true`.
- `--step NAME` restricts post to some steps.
- `--priority N` (default 0): when cores are scarce, queued jobs launch highest priority first,
  then oldest task first. Running jobs are never preempted. Change it later with
  `featx task priority TASK N` (the whole runset; `--only-task` for one task), MCP
  `task_priority`, or ▲/▼ on the Tasks page. The caps are `placement.max_total_cores` (run) and
  `placement.max_build_cores` (build), live-editable with `featx config set` or the UI.

A run recipe only needs the keys that differ from the defaults. `featx recipe show NODE run`
prints the effective recipe. A minimal `recipes/run.yaml`:

```yaml
workload: spec17          # all 590 points unless points/benchmarks narrow them
benchmarks: [mcf]
W: 20000000
I: 20000000
env: {TRACERTL_WP_PICK: after}
```

Write it with `featx recipe set NODE run -f run.yaml`, or set keys inline:
`featx recipe set NODE run W=1000000 workload=spec06`.

## recipes — Recipe layering and merge rules

The effective recipe of (node, kind) is a deep merge of these layers; later layers win:

1. `featx/defaults/recipes/<kind>.yaml` (package defaults; see `featx help build|run|post`)
2. `workload:<name>`: the `run:` keys of the selected workload (run recipes only; see `featx help workloads`)
3. `$FEATX_ROOT/recipes/<kind>.yaml` (`root`)
4. each ancestor's `recipes/<kind>.yaml`, from the root down
5. the node's own `recipes/<kind>.yaml`
6. `overrides`: `featx submit --set` (or the UI submit form)

Merge rules:
- dicts merge key by key;
- lists replace;
- `key: null` deletes the inherited key. `points: null` drops an inherited point list, and
  `steps: {pk: null}` drops an inherited post step.

`featx recipe show NODE KIND --explain` prints the effective recipe and the layer that set each
top-level key. Substitution only replaces `${name}` where `name` is a known variable (see
`featx help variables`), so shell syntax like `${HOME}` passes through.

## build — Build recipe keys

| key | default | meaning |
|---|---|---|
| `tool` | `gsim` | `gsim` (two-step gsim build), `verilator` (`make emu`) or `script`. The legacy `type: gsim-two-step\|script` still works. |
| `mode` | `trace` | `trace` or `exec` → `TRACERTL_MODE=1\|0`. A run of this build follows it. |
| `options.chiseldb` | `false` | `WITH_CHISELDB` of the Chisel elaboration (gsim step 2 always links the DB bridge) |
| `options.chiselmap` | `false` | adds `WITH_CHISELMAP=1` when true (a placeholder: no Makefile reads it yet) |
| `options.constantin` | `false` | `WITH_CONSTANTIN` |
| `options.dramsim3` | `true` | `WITH_DRAMSIM3` |
| `options.threads` | `1` | `EMU_THREADS`; a run's `cores_per_point` defaults to it |
| `options.optimize` | `-O3` | `EMU_OPTIMIZE` |
| `options.flags` | `{}` | other make variables, e.g. `{DISABLE_PERF: 1}` |
| `options.extra` | `""` | raw make args appended to every make call |
| `gsim.step1_args` / `gsim.step2_args` | `""` | raw args after the generated ones (they win) |
| `gsim.reuse_build_from` | `null` | for C++-only changes: copy this `build/` dir and only relink (step 2) |
| `gsim.gsim_bin`, `gsim.dramsim3_home`, `gsim.jobs` | | gsim binary, DRAMsim3, `make -j` |
| `verilator.args` | `""` | raw make args after the generated ones |
| `verilator.bin_dir`, `verilator.dramsim3_home`, `verilator.jobs` | | prepended to PATH, DRAMsim3, `make -j` |
| `script` | `null` | `tool: script`: a bash body run in `workdir` that must leave `artifact` |
| `workdir` | `${worktree}` | needs the node's own worktree (`featx worktree create NODE`) |
| `artifact` | `build/emu` | relative to `workdir`; copied into the bincache |
| `cache` | `true` | reuse `bincache/<key>` when commit + diff + submodules + recipe match |
| `patches` | `[]` | `{file, find, replace}` text patches applied for the build, then reverted |
| `env` | `NOOP_HOME`, `NEMU_HOME` | exported before the build |
| `cores`, `mem_gb`, `host`, `timeout_hours` | 64, 96, null, 10 | placement and limits |

At submit, `tool`/`mode`/`options` are lowered to the concrete make args. The task spec shows
both. The bincache key uses the lowered args, so the defaults keep the key that builds had before
presets. Examples:

```bash
featx submit NODE build --set build.mode=exec
featx submit NODE build --set build.tool=verilator --set build.options.chiseldb=true
```

## run — Run recipe keys

| key | default | meaning |
|---|---|---|
| `mode` | `null` | `trace` or `exec`. Unset: the build's mode (same submit, the `--bin` runset's build, or the bincache manifest's `mode`), else `trace`. A mismatch with the build is an error. |
| `workload` | `null` | `spec06`, `spec17`, `spec26` (see `featx help workloads`) |
| `points`, `points_file`, `suite`, `benchmarks`, `coverage` | | point selection (see `featx help points`) |
| `W`, `I` | 20000000, 40000000 | warmup and measured instructions (`-W`, `-I`) |
| `emu` | `${bin}` | the runset binary; or an absolute path |
| `args` | `""` | extra emu arguments |
| `env` | `{}` | exported before the emu, e.g. `{TRACERTL_WP_MEM: future}` |
| `trace_root`, `trace_file` | spec06 v1 | trace mode input; `trace_file` is a template over the point vars |
| `checkpoint` | `null` | exec mode: a checkpoint set name (`featx ckpt list`) or set dir; it gives the points and their images (see `featx help checkpoints`). `null` = the workload's own set via `checkpoint_file` |
| `checkpoint_file` | the workload's | exec mode input (`-i`); the workload sets it; a `checkpoint` set replaces it |
| `exec.diff` | `${paths.nemu_diff}` | NEMU `.so` for `--diff <so>`; empty gives `--no-diff`. `paths.nemu_diff` unset = `<git.repo>/ready-to-run/riscv64-nemu-interpreter-so` when that exists |
| `exec.gcpt_restorer` | `gcpt.bin` | `-r <bin>`, passed only for `.gz` checkpoints |
| `dramsim3_home` | DRAMsim3 checkout | `<dir>/build` is put on `LD_LIBRARY_PATH` (the emu links libdramsim3); `null` = none |
| `dump_db` | `false` | `--dump-db --db-path=<point dir>/chiseldb.sqlite` (needs `build.options.chiseldb`) |
| `cores_per_point` | `null` | `null` = the build's `options.threads` (1 without a build) |
| `mem_gb_per_point`, `timeout_hours` | 8, 72 | placement and limits |
| `completion_marker` | `true` | rc 0 without a marker → append "EXCEEDING CYCLE/INSTR LIMIT" (the collector needs it) |
| `command` | `null` | a custom command line; `null` = `commands[mode]` |
| `commands.trace` / `commands.exec` | | per-mode command templates |
| `executor` | `points` | `points` (featx places every point) or `perf_trigger` (`perf_trigger.*` keys) |

Each point runs in `run/<point>/`. Its stdout goes to `simulator_out.txt` and its stderr to
`simulator_err.txt`. The inputs (trace file, or checkpoint and diff `.so`) are checked before the
emu starts.

## points — Point selection rules

A point name is `<bench>_<cp>_<weight>`; the bench may itself contain `_`.

1. The selected set is `points` ∪ `points_file` (one name per line, `#` comments) ∪ `suite`
   (`{config: <spec json>, benchmarks: [...]}` or just the JSON path).
2. If none of these select anything, an exec run with `checkpoint` takes all of the set's points;
   otherwise, if a `workload` is set, the run takes all of the workload's manifest points.
3. `benchmarks: [...]` then keeps only the points of these benchmarks. Names are the manifest's
   `benchmark` column (`mcf`, not `505.mcf_r`), or a prefix of a benchmark up to `_` / `.`
   (`500.perlbench` selects `500.perlbench_rate_refrate_00_checkspam`). Submit fails if the filter drops a point named in
   `points` (or `--point`), or leaves nothing of a non-empty selection; the error lists the
   benchmarks it saw. An inherited filter usually needs `--set run.benchmarks=null`.
4. `coverage: c` (0 < c ≤ 1) keeps, per benchmark, the heaviest points (ties: lower checkpoint
   first) until their weights sum to at least `c`. `coverage: 0.3` on spec06/spec17 gives exactly
   the `spec06-0.3c.json` (143 points) / `spec17-0.3c.json` (70 points) sets. Names keep their
   weights, so baselines still match. It can't be combined with explicit `points`/`--point`;
   `null` or `1` = no filter.
5. With a checkpoint set, every point must be one of the set's; otherwise, with a workload, every
   point must be in its manifest. Otherwise submit fails and names the
   missing points; an inherited `suite`/`points` usually needs `null` in the node's layer.
6. `featx submit --point NAME` replaces `points`, `points_file` and `suite` for that submit.
   `benchmarks` and the workload check still apply to the names it gives.

Duplicates are dropped; the order is kept. `run/points.json` (`${points_config}`) records the
final set in the collector's JSON format.

## workloads — Workloads (featx.yaml `workloads`)

| name | points | label | reference baseline |
|---|---|---|---|
| `spec06` | 1090 (55 benchmarks) | `spec06` | `exec-0903` |
| `spec17` | 590 (31 benchmarks) | `spec17` | `exec-0903-spec17` |
| `spec26` | 1529 | `spec26` | none: post steps that compare need `with.reference` |

Each entry has:
- `manifest`: a TSV (`suite benchmark point weight checkpoint trace max_instr`);
- `label`: the analysis `--suite` label;
- `reference`: the default baseline of post comparisons;
- `insts`: per-benchmark instruction counts `{bench: {insts, points}}` for `spec-score`
  (spec06: the checkpoint `cluster-0-0.json`; spec17: `featx/defaults/insts/`, taken from the
  NEMU profiling logs; spec26: the checkpoint `checkpoints_all.json`; relative paths are under
  `featx/defaults/`);
- `checkpoint`: the checkpoint set the traces were made from (`featx ckpt list` shows it as
  `traced_by`); an exec run without `run.checkpoint` is recorded and compared as that set;
- `run:` keys, which become a recipe layer right above the package defaults, so any recipe
  layer still wins.

The manifest gives `${point.trace}` and `${point.checkpoint}`. Add or change a workload in
`$FEATX_ROOT/featx.yaml` (`featx config edit`).

## post — Post recipe (steps)

```yaml
steps:
  pk: {plugin: perf-keys, baselines: [exec-0903, wpe/overlay/smoke/20261013-3]}
  col: {plugin: collect}
  pr: {plugin: pair, after: [col], with: {reference: /some/collected/dir}}
  stats: {cmd: "bash ${scripts}/stats.sh ${run} ${out}", after: [pk]}
```

- The key is the step name. Without `plugin:` the step name is the plugin name.
- `with:` overrides the plugin's parameters (see `featx help plugins`).
- `after:` lists the steps this one waits for. The default is the plugin's `after`; only steps
  present in the recipe count.
- `baselines:` as a list: a baseline name, a runset ref (`node/YYYYMMDD-n` → its run dir), an
  `@` reference, or an absolute dir. The label is the baseline name or `node:runset`.
- `cmd:` runs an inline command instead of a plugin. It must leave `${out}/summary.json`, or
  name a JSON file with `summary_from:` that featx flattens into `summary.json`.
- `name: null` drops an inherited step.

`summary.json` keys appear as `<step>.<key>` metrics on the leaderboard and in `featx result`.

`pair`, `diagnose` and `branch-mpki` first check that the runset and the reference share at least
one valid point (an `ipc` in both collector `points.csv`). If they share none, the step fails
(exit 3) with `check_paired: nothing to compare: ...`. The message says which side has no valid
point and gives per-side counts with the collector status. A common cause is `I < W`: there is no
formal-window PERF block, so the status is `parse_error:need 2 PERF blocks`.

## variables — ${variables}

Available in every recipe:

| variable | value |
|---|---|
| `${node.path}`, `${node.name}`, `${node.id}`, `${node.dir}` | the node |
| `${featx.root}`, `${featx.home}` | `$FEATX_ROOT`, the featx code dir |
| `${util}`, `${tools}` | featx's `bin/` (runwatch.sh, add-worktree.sh); the analysis scripts (`analysis_dir`, default the copy shipped in `featx/post/scripts/analysis`) |
| `${date}` | today, `YYYYMMDD` |
| `${worktree}`, `${repo}`, `${branch}`, `${sub.<name>}` | the node's own worktree (unset without one) |
| `${scripts}` | the runset's snapshot of the inherited helper scripts |
| `${runset.dir}`, `${runset.name}`, `${bin}` | the runset, and its `bin/emu` |

Per point (run commands): `${point.name}`, `${point.bench}`, `${point.cp}`, `${point.weight}`,
`${point.trace}` and `${point.checkpoint}` (from the workload manifest), plus:
- `${emu}`, `${W}`, `${I}`, `${args}`, `${trace_root}`;
- `${trace_file}` (trace mode), `${checkpoint_file}`, `${diff_args}`, `${restorer_args}` (exec mode);
- `${db_args}`.

Post steps:
- `${out}` (this step's dir), `${run}` (the runset's point dirs), `${points_config}`;
- `${post.<step>}`, `${step}`, `${python}`, `${plugin.scripts}`;
- every `with:` parameter;
- `${workload}`, `${workload.label}`, `${workload.reference}`, `${workload.insts}`. These are
  taken from the runset's run recipe; an exec run with only a `checkpoint` set uses the workload
  of the same SPEC version; otherwise the `default_workload`;
- `${checkpoint}` (the run's checkpoint set, explicit or the workload's) and
  `${checkpoint.insts}` (that set's `{bench: {insts, points}}` json; without one, `${workload.insts}`
  for the workload's own set and empty for an explicit `run.checkpoint` set).

Shell environment of every job:
- `$FX_TMP`: a node-local temp dir;
- `$FX_JOB_DIR`: the job dir;
- `$FX_ATTEMPT`: the attempt number;
- `$FX_DIR`: `.fx/<attempt>` of the job;
- post steps also get `$FEATX_ROOT` and `$FEATX_TOOLS`.

## references — @references in post parameters

| form | resolves to |
|---|---|
| `@baseline(NAME)` | the baseline's result root (`featx baseline list`) |
| `@baseline(NAME).collected` | its collected dir (the `pair`/`diagnose` input) |
| `@run(REF)` | `<runset>/run` of a runset ref (`node/YYYYMMDD-n` or an id) |
| `@runset(REF)` | the runset dir |
| `@post(REF, STEP)` | `<runset>/post/<STEP>` |
| `self` inside `@run(self)`, `@runset(self)`, `@post(self, STEP)` | this runset |

`${workload.reference}` is substituted first, so `@baseline(${workload.reference})` follows the
workload. A workload without a reference fails the step with "no reference baseline". In that
case set `with.reference` or `baselines` explicitly.

## plugins — Post plugins

Manifests live in `featx/post/manifests/`, and `$FEATX_ROOT/plugins/` overrides or extends them.
Keys:
- `name`, `description`;
- `with` (default parameters, which may use variables and references);
- `params` (what each parameter means);
- `after` (default dependencies);
- `cmd` (bash template);
- `outputs`;
- `summary_from`.

`featx plugin list` and `featx plugin show NAME` show what is installed.

<!-- plugins:begin (generated: python -m featx.docs) -->
### branch-mpki

extract_branch_type_mpki.py: per-point branch-type MPKI, this runset vs a reference.

Runs after: `collect` (when that step is in the recipe).

| param | default | meaning |
|---|---|---|
| `trace` | `${post.collect}/points.csv` | this runset's collected points.csv |
| `reference` | `@baseline(${workload.reference})` | reference (default the workload's reference baseline) |
| `config` | `${points_config}` | points (default this runset's points) |
| `suite` | `${workload.label}` | label (default the run's workload label) |

Outputs (in `${out}` = `<runset>/post/<step>/`):

- `branch_type_mpki.csv`: per-point MPKI per branch type, trace vs reference
- `summary.json`: mpki.rows, mpki.mean.<numeric column>

### collect

collect_gsim_spec_perf.py over the runset's point dirs (formal window -N 2): points.csv, benchmarks.csv, suite.json (-> summary.json).

| param | default | meaning |
|---|---|---|
| `input` | `${run}` | dir of <bench>_<cp>_<weight> point dirs (default this runset's run/) |
| `config` | `${points_config}` | collector JSON restricting the points (default this runset's points) |
| `suite` | `${workload.label}` | label written into the outputs (default the run's workload label) |
| `N` | `2` | PERF block to read (2 = formal window after warmup) |
| `extra` | `""` | raw collector args, e.g. --include-incomplete |

Outputs (in `${out}` = `<runset>/post/<step>/`):

- `points.csv`: one row per point (ipc, branch_mpki, topdown_*, train counters, ...)
- `benchmarks.csv`: weighted per-benchmark rows
- `suite.json`: -> summary.json: configured_points, completed_points, ipc_geomean_completed_benchmarks, benchmark_mean_<metric>, completed_point_total_<counter>, completed_point_wpt_fraction, ...
- `collect.stdout`: collector log

### diagnose

diagnose_top_ipc_gaps.py: per-point TopDown reports for the N largest IPC gaps vs a reference (ranking.csv, topdown_nodes.csv, summary.md, points/*.md).

Runs after: `collect` (when that step is in the recipe).

| param | default | meaning |
|---|---|---|
| `trace` | `${post.collect}/points.csv` | this runset's collected points.csv |
| `reference` | `@baseline(${workload.reference})` | reference (default the workload's reference baseline) |
| `config` | `${points_config}` | points (default this runset's points) |
| `suite` | `${workload.label}` | label (default the run's workload label) |
| `top_n` | `10` | how many largest-IPC-gap points get a report |
| `repo_root` | `${paths.xiangshan}` | XiangShan checkout for the TopDown definitions (default paths.xiangshan, else git.repo) |

Outputs (in `${out}` = `<runset>/post/<step>/`):

- `ranking.csv`: points ranked by IPC gap
- `topdown_nodes.csv`: TopDown node deltas of the ranked points
- `summary.md`: human summary
- `points/<point>.md`: per-point report
- `summary.json`: ranking.rows, ranking.mean.<numeric column of ranking.csv>

### pair

compare_gsim_spec_points.py: pair this runset's collected points with a reference (baseline name / point-dir root / collector dir; raw roots are collected once and cached under $FEATX_ROOT/compare/collect-cache restricted to this runset's points).

Runs after: `collect` (when that step is in the recipe).

| param | default | meaning |
|---|---|---|
| `trace` | `${post.collect}/points.csv` | this runset's collected points.csv (default ${post.collect}/points.csv) |
| `reference` | `@baseline(${workload.reference})` | point-dir root, collector dir or @baseline/@run ref (default the workload's reference baseline) |
| `config` | `${points_config}` | points to pair (default this runset's points) |
| `suite` | `${workload.label}` | label (default the run's workload label) |
| `topdown_config` | `${paths.xiangshan}/scripts/top-down/configs.py` | XiangShan top-down configs.py (default from paths.xiangshan, else git.repo) |

Outputs (in `${out}` = `<runset>/post/<step>/`):

- `points.csv`: paired trace/execution rows per point, sorted by \|IPC delta\|
- `benchmarks.csv`: per-benchmark trace_/execution_<metric> and deltas
- `topdown_points.csv`: per-point TopDown category deltas
- `topdown_benchmarks.csv`: per-benchmark TopDown category deltas
- `suite.json`: -> summary.json: paired_points, paired_benchmarks, trace_ipc_geomean, execution_ipc_geomean, execution_ipc_change, trace_mean_<metric>, execution_mean_<metric>

### perf-keys

Formal-window (2nd PERF block) key counters per point for this runset and any number of references (baselines / other runsets), IPC error vs the first reference (generalizes the 1007/1009 summarize.py + compare-panel.py). Writes points.csv, report.md, summary.json (per run: n, mean|err|, mean err, IPC geomean).

| param | default | meaning |
|---|---|---|
| `self` | `${run}` | this runset's point dirs (label "self") |
| `baselines` | `{'exec': '@baseline(${workload.reference})'}` | label: point-dir root map (or a list of baseline names / runset refs); the first is the IPC reference (default exec: the workload's reference baseline) |
| `points` | `${points_config}` | collector JSON or one point name per line (default this runset's points) |
| `block` | `2` | PERF block (2 = formal window after warmup) |
| `keys` | `""` | optional YAML with extra keys [[name, module.suffix, counter], ...] |

Outputs (in `${out}` = `<runset>/post/<step>/`):

- `points.csv`: per point and run, the key counters
- `report.md`: per-run error table
- `summary.json`: reference, block, points, common_points, <label>.finished, <label>.ipc_geomean, <label>.mean_abs_err_pct, <label>.mean_err_pct, <label>.max_abs_err_pct

### spec-score

SPEC score the way perf_trigger --report computes it (finished points only, cpi from the last clock_cycle/commitInstr, per-benchmark time = weighted cpi x insts / f, group score = ref_time / time / f, SPECint/fp/overall geomean per GHz and @f GHz, minimal coverage), for this runset and optionally baselines on the same point set. Writes groups.csv, benchmarks.csv, report.md, summary.json.

| param | default | meaning |
|---|---|---|
| `self` | `${run}` | this runset's point dirs (label "self"; its scores are the top-level summary keys) |
| `baselines` | `{}` | label: point-dir root map (or a list of baseline names / runset refs), scored on the same points; adds <label>.spec_per_ghz and <label>.score_err_pct |
| `points` | `${points_config}` | collector JSON or one point name per line (default this runset's points) |
| `insts` | `${checkpoint.insts}` | per-benchmark instruction count json {bench: {insts, points}} (default the run checkpoint set's json; a run without run.checkpoint falls back to the workload's featx.yaml `insts`, a take_cpt run.checkpoint set has none, so pass it here; weights listed there are used as perf_trigger uses them) |
| `frequency` | `3.0` | GHz used for time and score |
| `spec_version` | `auto` | auto \| 06 \| 17 \| 26 |
| `label` | `${workload.label}` | workload label for spec_version auto (default the run's workload label) |

Outputs (in `${out}` = `<runset>/post/<step>/`):

- `groups.csv`: per run and SPEC benchmark, time, ref_time, score, coverage
- `benchmarks.csv`: per run and benchmark, insts, finished points, weights, weighted cpi, time
- `report.md`: perf_trigger-style score table per run
- `summary.json`: spec_per_ghz, specint_per_ghz, specfp_per_ghz, spec_at_freq, min_coverage, spec_per_ghz_available, groups_scored, missing_groups, finished, points, common_points, score.<group>, <label>.spec_per_ghz, <label>.score_err_pct
<!-- plugins:end -->

## worktrees — Worktrees and branches

- `featx worktree create NODE` creates `<wt_root>/<node path>` on branch `<node path>`. The
  setting `git.branch_prefix` (default `""`) is prepended to the branch name. Listed submodules
  (default TraceRTL) get their own worktrees on the same branch.
- If a branch with that name exists, or a branch that is a path prefix of it (git can't hold both
  `a` and `a/b`), create is refused before anything changes. Pass `--branch NAME` in that case.
- `--adopt --path DIR` registers an existing worktree. featx then never renames or deletes its branch.
- `featx node mv` moves worktrees and renames the branches featx created. On a name clash it
  keeps the old branch name and reports it.
- `featx worktree rename-branch NODE [--to NAME] [--dry-run]` renames a featx-created branch
  and its submodule branches, e.g. an old `featx/<path>` to `<path>`.
- `featx node rm NODE -f --drop-worktrees` deletes worktrees and featx branches without their
  own commits. Branches with commits are kept unless you also pass `--drop-branches`.

A build needs the node's **own** worktree (`${worktree}`). A run with `--bin` doesn't.

## baselines — Baselines

A baseline is a named result root, a directory of `<point>/simulator_out.txt` dirs, kept in
`$FEATX_ROOT/baselines.yaml`.

```bash
featx baseline list
featx baseline add NAME ROOT -m "description" [--collected DIR] [--binary EMU]
featx baseline rm NAME [--force]     # refused while a recipe refers to it, unless --force
```

Older roots spell weights shortened (`GemsFDTD_10619_0.03475`); `perf-keys` matches such a dir to
the 6-digit point name (`..._0.034750`) by numeric weight.

The site profile's baselines (nfs-share: `exec-0903` for SPEC06, `exec-0903-spec17`) are
overlaid per entry by `$FEATX_ROOT/baselines.yaml`; `rm` of a site entry hides it. Post steps
refer to a baseline as `@baseline(NAME)` or by name in a `baselines:` list.

An optional `checkpoint:` field names the checkpoint set the baseline ran. When a post step
compares a runset with a baseline or another runset of a different set, it prints a warning and
its `summary.json` gets `ckpt_mismatch: true` (+ `ckpt_mismatch_with`); the point overlap is
compared as usual.

## checkpoints — Checkpoint sets (exec mode)

`checkpoints.roots` (featx.yaml; the nfs-share site lists `/nfs/home/share/checkpoints_profiles`
and its `legacy/`) holds the SimPoint checkpoint sets. A sub-directory whose name contains
`spec<NN>` is a set; the name is its version.

```bash
featx ckpt list [--spec 06] [--all] [--json]   # runnable sets; --all adds multi-hart / broken ones
featx ckpt show SET [--bench mcf]              # points, weights, image files
featx ckpt rescan                              # rebuild the cached indexes
featx submit NODE run --bin <exec emu> --set run.mode=exec \
    --set run.checkpoint=spec06_gcc16_rva23_novec_260820 [--set run.coverage=0.3]
```

Layouts, in this order:

| layout | images | weights json |
|---|---|---|
| modern | `checkpoint/<bench>/<cp>/_<cp>_<w>[_memory_].zstd` | `json/checkpoints_all.json` (with insts) |
| legacy0 | `checkpoint-0-0-0/<bench>/<cp>/_<cp>_<w>_.zstd` | `checkpoint-0-0-0/cluster-0-0.json` (with insts) |
| take_cpt | `take_cpt/<bench>_<cp>_<w>/0/_<cp>_.gz` or `take_cpt/<bench>/<cp>/_<cp>_<w>_.gz` | `simpoint_summary.json` (weights only) |

The weights json decides the points; each needs its image. A point is `<bench>_<cp>_<w>` with the
weight at 6 decimals, as in the trace manifests and result dirs. Images in a dir but not in the
json (tiny weights the profiler dropped) are counted as `extra` and not used.

With `run.mode: exec` and `run.checkpoint: SET`:
- the points are the set's (`benchmarks`, `coverage`, `points` select among them; see
  `featx help points`), and `${checkpoint_file}` is each point's image;
- `.gz` images get `-r ${exec.gcpt_restorer}`;
- `run/points.json` carries the set's insts, so `spec-score` weighs with them (take_cpt sets
  have no insts: `spec-score` then needs `with.insts`, another set's counts are never used);
- `experiment.yaml` records `run.checkpoint_set` (name, path, layout, weights json and its sha256);
- the leaderboard puts it in its own class (`ckpt=SET`).

Without `run.checkpoint`, an exec run uses the workload's `checkpoint_file` template unchanged
and is recorded as the workload's `checkpoint` set. Refused: a multi-hart set (featx runs
single-core emus), a broken set (dangling `take_cpt` link, no images), `run.checkpoint` in trace
mode other than the set the workload's traces were made from, `run.checkpoint` with
`executor: perf_trigger`, and a `run.workload` of another SPEC version than the set (the
workload labels and scores the run; set it to a matching workload or `null`).

featx does not check that a set's ISA (rv64gcb, rva23, vector) matches the emu; pick a binary
built for it. Indexes are cached in `$FEATX_ROOT/cache/ckpt/` and rebuilt when a set's dir,
weights json or image dir changes.
