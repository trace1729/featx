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
  - `experiment.yaml`: provenance (commit, diff hash, submodules), the resolved recipes, `bin`,
    the raw submit `overrides` and the `archived` flag;
  - `bin/emu`: the binary the run used (build output, or the `--bin` binary);
  - `build/`, `run/<point>/` (one dir per point: `simulator_out.txt`, `simulator_err.txt`, `env.txt`),
    `post/<step>/` (each step's `summary.json` and reports);
  - `scripts/`: the snapshot of the inherited helper scripts;
  - `problem-analysis.log`: the analysis entries (`featx log append`).
  A runset is sealed once all its tasks succeed. A rerun after a fix is a new derived runset
  (`featx task resubmit`). A runset can be archived (hidden from the lists, data kept) and its
  build records the source it compiled (HEAD + a saved patch per repo) — see the `runsets` topic.
- **Task**: one kind (build, run or post) of a runset, made of jobs. A build is one job, a run
  has one job per point, a post has one job per step. The tasks of a submit are chained:
  build → run → post. A queued task whose dependency failed is cancelled (note
  `dependency <kind> <id12> failed`, `cancelled_by` names the upstream) and revived when that
  upstream task is relaunched.
- **Node fields** (`featx node update`, MCP `node_update`, the UI header): `hypothesis`,
  `verdict` (open|supported|refuted|inconclusive), `verdict_note`, `status`
  (active|parked|done|abandoned), `evidence` (a list: `--evidence` adds, `--rm-evidence` removes;
  the API also accepts the full list), `tags`, `notes` (Markdown, the Notes tab) and `comment`
  (free-form thoughts). Other keys (git, moves, created) cannot be changed this way.
- **Code changes**: `node_context.git` reports `ahead` and `files_changed` against the base;
  `featx worktree changes NODE [--json]` lists committed and working-tree changes against each
  repository's merge-base. The UI worktree tab and `GET /worktrees/changes?ref=NODE` expose the
  same data, and `GET /worktrees/diff?ref=NODE&sub=&path=` returns one file's diff. Submodule
  gitlinks are excluded from the superproject and shown separately.
- **Node display**: `featx node show NODE` is a concise text view of the research record and
  recent runs. `--json` emits that brief view as JSON; `--full` selects the complete context as
  YAML, and `--full --json` selects the complete JSON context used by MCP. `--runsets 0` hides
  runsets. Hypothesis, verdict, verdict note, evidence, and comment stay in the concise view.
- **Ref forms**: a node is its path (`wpe/overlay/smoke`) or its id. A runset is
  `<node path>/<YYYYMMDD-n>` or a runset id prefix (≥ 6 characters).

## runsets — Parameters, archive and code records

**Parameters.** Each runset keeps the resolved recipe snapshots it submitted (`Runset.recipes`)
and the raw `--set` overrides (`Runset.overrides`). `featx result REF` prints them as a
build/run/post parameter block and `GET /result` returns `params`, `recipes` and `overrides`;
`GET /nodes/context` also returns `params_differing`, the recipe keys on which the node's
runsets disagree (runset directories, `${scripts}` paths and timestamps are ignored). The Runset
page shows them as a card with the overridden keys marked. The Node page's params column shows
only the values that differ between the listed runsets, on one line (`+N` counts the rest; the
tooltip has every headline value and the differing recipe keys); "same" means nothing differs.
The Runset page's jobs tab filters jobs by state (chips with counts, kept in `?jobs=`).

`featx result` (and MCP `result_get`) is a digest by default: tasks, `post` metrics, `code`,
`review`, and `failures` (failed points, capped) — the submit-time echo is dropped
(per-submodule `provenance`, `recipes`, `overrides`, report text, the benchmark-name list) and
replaced by counts plus `sections_hint`. `--provenance`, `--recipes`, `--overrides` and
`--reports` add one section back, `--full` returns everything; `GET /result` stays full by
default for the UI and takes `detail=summary|full` plus `sections=provenance,recipes,...`.

**Archive.** Archived runsets are hidden by default from the node runsets list,
`node_context`, `tree_list`'s latest runset and the leaderboard. Data and binaries are kept and
`featx gc` does not change. It is refused while the runset has active tasks. The node page can
archive multiple selected runsets through the existing single-runset API.

```bash
featx runset archive wpe/overlay/20261012-1 -m "duplicate of 20261012-2"
featx runset archive wpe/overlay/20261012-1 --undo
featx node runsets wpe/overlay --all          # include archived ones
```

REST: `POST /runsets/archive {ref, archived, note}`; MCP: `runset_archive`. `include_archived`
shows them again in `GET /runsets`, `GET /nodes/context` and `GET /leaderboard`
(`node_context.archived_count` is always reported).

**Code records.** A build records its source when it is materialized, without committing
anything: for the superproject and every submodule, the HEAD it is on plus its uncommitted
changes, saved as `<runset>/code/<repo>.patch` (`super.patch`, `<sub with / as __>.patch`). The
patch holds the tracked edits and the small untracked (not ignored) files. Untracked files that
look like build output are not saved but listed as `omitted`: a wholly untracked directory of
more than 50 files (e.g. `build-vlt-1027/`), files over 1 MiB, symlinks, and anything beyond
200 files / 8 MiB in total. `provenance.code` in `experiment.yaml` holds per repo `head`,
`branch`, `base`, `merge_base`, `patch`, `files/added/deleted`, `untracked` and `omitted`, and
`provenance.drift` says when it no longer matches the binary:

- `after submit`: the tree moved before the build launched, so it was re-recorded and re-keyed;
- `during build`: the tree moved while compiling, so the binary is *not* stored in the bincache
  (it would sit under a key that describes other sources); the runset still uses it.

The code view rebuilds each side (HEAD + patch) as a git tree in a throwaway object directory
(the repo's objects are only read), so your repos, index and refs are never written. A recorded
HEAD that is later rebased away stays viewable until git prunes it; then the repo reports
"no longer in this repo". The Node page's code column shows the HEAD and an `+N uncommitted`
label; the Runset page's code tab shows the record and the per-file diffs (files from the patch
are marked uncommitted).

```bash
curl -H "X-Featx-Token: $TOKEN" "$URL/runsets/code?ref=wpe/overlay/20261012-1&against=base"
```

`GET /runsets/code?ref=R&against=base|prev|worktree|<runset ref>` returns the same shape as
`/worktrees/changes` (`repos.*.files/commits`) plus `record` (per repo what was recorded),
`drift`, `source_runset` and `total`; `GET /runsets/code/diff?ref&against&sub&path` returns one
file's diff. The `against` sides:

- `base`: what the runset's code adds on top of the node's base branch (merge-base → runset);
- `prev` / a runset ref: what changed from that runset's code to this one's;
- `worktree`: what the live worktree changed since this runset's code (runset → worktree now).

`prev` is the closest earlier runset of the node with recorded code. A runset built without a
build (`--bin`) points at the runset whose bincache entry built that binary; an imported
`ext-` binary has no code view. Older runsets (and the few built with the short-lived
`refs/featx/runsets/*` snapshots) use the HEADs recorded at submit plus `src-patches/*.patch`
(tracked changes only; their untracked files were never saved). MCP: `runset_code`.

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
- `--dry-run` runs the real recipe/preset/point validation and prints the resolved tasks and
  commands without creating a runset, tasks, script snapshots, links, or events. Preview-only
  IDs and the `dry-run` runset path are marked as such. A bincache key that needs a future script
  snapshot is reported as unavailable rather than guessed.
- `--priority N` (default 0): when cores are scarce, queued jobs launch highest priority first,
  then oldest task first. Running jobs are never preempted. Change it later with
  `featx task priority TASK N` (the whole runset; `--only-task` for one task), MCP
  `task_priority`, or ▲/▼ on the Tasks page. The caps are `placement.max_total_cores` (run) and
  `placement.max_build_cores` (build), live-editable with `featx config set` or the UI.
- Builds sharing a canonical `build.workdir` may all be submitted, including with `--after`.
  They wait in the queue and are serialized at dispatch; two compilation jobs never use one
  workdir simultaneously. Different workdirs can build in parallel, and simulations keep using
  their cached emulators.

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

Trace is disabled by default (`trace_enabled: false`). Builds/runs default to exec,
TraceRTL worktree management and trace checks in doctor are skipped, and points are
resolved from checkpoint sets without trace manifests. Enable trace explicitly with
`featx init --trace` or `featx config set trace_enabled=true`. `--no-trace` disables it. Restart the daemon after
changing this setting. The nfs-share profile defaults `paths.dramsim3` to
`${share}/ci-workloads/DRAMsim3` (with `build/`), with shared-path fallback.

When trace is disabled, effective settings and recipe layers omit trace manifests,
`trace_root`, `trace_file`, `commands.trace` and TraceRTL environment variables.
The trace run defaults are loaded from `defaults/recipes/trace-run.yaml` only when enabled.
Stored user and node layers retain their values so trace can be enabled again.
Effective recipes can contain local paths from your private site, node overrides or
`${paths.*}` expansion; `featx recipe show NODE run --explain` shows their source layers.

| key | default | meaning |
|---|---|---|
| `tool` | `gsim` | `gsim` (two-step gsim build), `verilator` (`make emu`) or `script`. The legacy `type: gsim-two-step\|script` still works. |
| `mode` | `trace` | `trace` or `exec` → `TRACERTL_MODE=1\|0`. A run of this build follows it. |
| `pipeline` | `difftest` | `difftest` (the builds above) or `tracertl`: a trace-mode gsim/verilator emu built by TraceRTL's own entry, `make -C <tracertl.dir> MODE=xiangshan SIM=<tool> emu`, over the unmodified difftest (no `TRACERTL_MODE`) |
| `tracertl.dir`, `tracertl.args` | `TraceRTL`, `""` | `pipeline: tracertl`: the TraceRTL dir in `workdir`, raw make args after the generated ones; paths and `-j` come from the tool's `gsim:` / `verilator:` section |
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

The nfs-share profile defaults `paths.perf_trigger` to
`${share}/ci-workloads/env-scripts/perf_trigger/main.py`, trying `/nfs/share` first and
`/nfs/home/share` when the path is missing. Override `paths.perf_trigger` for another checkout.

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
| `spec06` | 1090 (55 benchmarks) | `spec06` | configure your reference |
| `spec17` | 590 (31 benchmarks) | `spec17` | configure your reference |
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
  pk: {plugin: perf-keys, baselines: [exec-reference, wpe/overlay/smoke/20261013-3]}
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

## review — Hypothesis, experiment and review

A hypothesis is recommended when creating a node, but remains optional. The CLI
prints a warning to stderr and the web dialog shows a hint; neither blocks creation.

When every task in a runset is terminal (`succeeded`, `failed` or `cancelled`), the
runset needs review if its node's verdict is `open` or its evidence list is empty.
`featx result REF` shows a reminder and a suggested command:

```bash
featx node set NODE --verdict inconclusive --evidence NODE/YYYYMMDD-N
```

Choose the verdict yourself (`supported`, `refuted`, or `inconclusive`) and record
what the experiment supports, including failures and cancellations. Task outcomes
never update verdict or evidence automatically. Web runset badges and banners link
to the existing node editor; refreshing after both fields are recorded clears the
reminder. Review is a node-level record, not a separate review of each runset.

The scheduler emits one `review_needed` event on each transition to all-terminal
when review is needed. Recovery and read-only queries do not emit reminders;
relaunching a task permits a new reminder when it finishes again. The additive
`review` object in result/runset summaries includes `needed`, `complete`, `outcome`,
`ref`, `verdict`, `evidence` and `command`. `result --json` keeps its existing fields
and metrics. Only all-success runsets are sealed; archive behavior is unchanged.

The web UI's EN / 中文 buttons save the language choice in this browser. Built-in
labels, dialogs and hints are translated; user content, logs, commands, paths,
recipe/metric keys and this reference remain unchanged. Unknown labels fall back
to English. worktree and note keep their original terminology.

MCP parity: `node_show(node, runsets=3, full=False)` matches `featx node show`;
`full=True` includes the complete context, also available through `node_context`.
`node_create` returns an additive `warnings` list when the hypothesis is blank.
`result_get`, `runsets_list` and node summaries expose computed review state.
`node_update(rm_tags=[...])` removes tags; `node_move(dry_run=True,
keep_branches=True)` supports move previews and keeping branch names.

## wait — Waiting for tasks

Use `featx wait TASK... --timeout 3600` or MCP `task_wait(tasks=[...], timeout=3600)`.
The default timeout is 86400 seconds; zero checks status once. Event long polling
watches the complete dependency chain: waiting for post alone also wakes when an
upstream build fails or is cancelled.

Results contain `finished` (terminal task views), `blocked` (queued tasks with
failed/cancelled dependencies, and tasks auto-cancelled because a dependency failed —
`cancelled_by` set — with the upstream IDs in `failed_dependencies`), `pending`
(IDs still active at timeout), and `timed_out`. Blocked task views retain their
actual scheduler state. Wait never changes tasks or acknowledges inbox events.
A repaired pipeline can be waited on again after relaunch.

The CLI exits 0 only when all requested tasks succeeded, otherwise 1.
`--json` returns the same result structure as MCP. For general event monitoring,
use `featx events --wait 600` / MCP `events_wait`.

`featx task show TASK` is a summary: state, per-state `counts`, ETA, and only the failed
jobs (`jobs_total`, `jobs_truncated`, and a runnable `jobs_hint`). `--jobs [--state
failed,running] [--limit N]` lists jobs instead (0 = no cap); MCP `task_status` takes
`detail="summary"|"jobs"`. REST `/tasks/{id}` stays full by default because the UI renders
every job, and accepts `detail=summary|jobs` plus `state`/`limit`.

After upgrading the event endpoint, use `featx daemon stop` then
`featx daemon start` and reconnect the MCP client. Running jobs are re-adopted
on daemon startup.

`featx daemon stop` sends TERM and waits 30 s; if the process is still there it sends KILL and
says so. A synchronous API handler stuck in a blocking call (e.g. an NFS read) makes uvicorn's
shutdown wait on task cancellation forever, so `featxd` arms a watchdog when shutdown starts and
leaves with `os._exit` after 20 s; `stop` writes the `stopped` mark for such hard exits. If
`daemon start` reports "already running" for a daemon whose API is unreachable, it also warns
that the process is half-stopped: run `featx daemon stop`.

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

Formal-window (2nd PERF block) key counters per point for this runset and any number of references (baselines / other runsets), IPC error vs the first reference (generalizes the 1007/1009 summarize.py + compare-panel.py). Writes points.csv, report.md, summary.json (per run: n, mean|err|, mean err, rms err, IPC MAE/RMSE, IPC geomean).

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
- `summary.json`: reference, block, points, common_points, <label>.finished, <label>.ipc_geomean, <label>.mean_abs_err_pct, <label>.mean_err_pct, <label>.max_abs_err_pct, <label>.rms_err_pct, <label>.ipc_mae, <label>.ipc_rmse (absolute IPC difference vs the reference)

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

## reap — Orphan featx-exec processes

A launch that was reported failed (an ssh timeout) or never started can still start later, and a
kill sent to an unreachable host can be lost. featx watches those launches (`liveness.orphan_watch`,
900 s), persists pending kills and watches to `$FEATX_ROOT/sched-pending.json`, retries kills to an
unreachable host every tick for `liveness.kill_retry_hours` (24 h), and kills a late start when it
writes its pid.

```bash
featx reap            # dry run: one ssh per host, lists this root's featx-exec processes
featx reap --apply    # kill the confirmed orphans (the whole session, emu included)
```

A process is an orphan when its job is not active (finished, cancelled, unknown) or its attempt is
stale. The daemon runs the same scan at start and every `liveness.reap_interval` (600 s), kills
only confirmed orphans and emits one `orphan` event per kill. REST: `POST /reap {apply}`; MCP:
`reap`.

## worktrees — Worktrees and branches

- `featx worktree create NODE` creates `<wt_root>/<node path>` on branch `<node path>`. The
  setting `git.branch_prefix` (default `""`) is prepended to the branch name. Listed submodules
  (default TraceRTL) get their own worktrees on the same branch.
- If a branch with that name exists, or a branch that is a path prefix of it (git can't hold both
  `a` and `a/b`), create is refused before anything changes. Pass `--branch NAME` in that case.
- `--adopt --path DIR` registers an existing worktree. featx then never renames or deletes its branch.
- Root and explicitly configured submodule bases are validated with `git rev-parse --verify`
  before create/adoption saves anything. Set one with `--base REF` and `--sub-base NAME=REF`;
  repair one later with `featx worktree set-base NODE REF [--sub NAME]`. An unresolved ref is an
  error instead of a later misleading code display. `worktree status` includes merge-bases.
- `featx worktree changes NODE [--json]` shows committed and uncommitted changes against the
  merge-base. “Clean” in APIs means no uncommitted changes, not no branch changes; detached
  submodule HEADs are normal. The web labels state these meanings explicitly.
- `featx node mv` moves worktrees and renames the branches featx created. On a name clash it
  keeps the old branch name and reports it.
- `featx worktree rename-branch NODE [--to NAME] [--dry-run]` renames a featx-created branch
  and its submodule branches, e.g. an old `featx/<path>` to `<path>`.
- `featx node rm NODE -f --drop-worktrees` deletes worktrees and featx branches without their
  own commits. Branches
  with commits are kept unless you also pass `--drop-branches`. A checkout featx only adopted
  (`worktree create --adopt`) is kept, and so is a worktree another node also records; both are
  reported under `kept` in the command output.

A build needs the node's **own** worktree (`${worktree}`). A run with `--bin` doesn't. Compilation
outputs are not per-runset: builds in one workdir are queued and serialized at dispatch, while
runset logs and cached emulators are isolated.

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

The public nfs-share profile ships without experiment baselines. Register your own with
`featx baseline add NAME ROOT` and set the workload's `reference` if needed. Custom site
profiles can provide baselines, overlaid per entry by `$FEATX_ROOT/baselines.yaml`; `rm` of a site entry hides it. Post steps
refer to a baseline as `@baseline(NAME)` or by name in a `baselines:` list.

An optional `checkpoint:` field names the checkpoint set the baseline ran. When a post step
compares a runset with a baseline or another runset of a different set, it prints a warning and
its `summary.json` gets `ckpt_mismatch: true` (+ `ckpt_mismatch_with`); the point overlap is
compared as usual.

## checkpoints — Checkpoint sets (exec mode)

`checkpoints.roots` holds the SimPoint checkpoint sets. The nfs-share site uses
`${share}/checkpoints_profiles` and its `legacy/`, preferring `/nfs/share` and falling back
to `/nfs/home/share` for missing paths (`share_fallbacks: []` disables fallback). A sub-directory whose name contains
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
