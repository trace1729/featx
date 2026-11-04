# AGENTS.md — featx agent rules

Use featx to preserve: **intent → code → experiments → evidence → conclusions**.
The feature tree is the source of truth; do not create untracked experiments in
ad-hoc directories. See `docs/reference.md` for full syntax and `intent.md` for
rationale. Keep this guide, both READMEs, and the reference in sync.

- `docs/reference.md` is a symlink to `featx/docs/reference.md`: one file, also
  served as topics by `featx help <topic>`, MCP `docs`, and the UI Docs page.
- This guide carries the rules and the canonical invocations; per-command flags
  live in `featx <cmd> --help`.

## 1. Never

- Mutate only after inspecting the node, worktree, tasks, Git status, and runsets;
  preserve user files and active experiments.
- Reset/clean/rebase/force-push, cancel, delete, gc, or restart without explicit
  authorization; `reap --apply`, `gc --apply`, `node rm`, `task cancel`, and
  `daemon stop` all need it. Dry-run and inspect first.
- Send `submit --dry-run` to a daemon older than the CLI: it may ignore the flag
  and submit for real. Check `featx daemon status` first.
- Call missing, pending, blocked, timed-out, or unverified work done.
- Kill processes by PID or name; use `featx reap`.
- Run tests against production state or read secrets (`.env`, tokens). Use a
  temporary `FEATX_ROOT` and temporary repositories.

## 2. Mental model

- **Node**: hypothesis, verdict, evidence, notes, Git info, recipes, and runsets.
- **Worktree**: checkout associated with a node; make repository changes there.
- **Runset**: dated experiment `<node>/<YYYYMMDD-n>` with actual inputs and outputs.
- **Task**: build, run, or post, normally build → run → post.
- **Job / attempt**: scheduled work and each execution attempt.
- **Evidence**: runsets/artifacts supporting a conclusion; never inferred from exit status.

The daemon owns scheduling and process state. Jobs survive the interactive session.

## 3. Inspect an exploration

```bash
source .venv/bin/activate
featx daemon status && featx config show effective
featx ps && featx tree
featx node show <node>                    # concise; --recipes, --runsets N (0 hides), --full, --json
featx worktree status <node>
featx worktree changes <node> [--json]
featx result <node>/<YYYYMMDD-n> [--json]
```

- `node show` is the concise research record; `--runsets 0` hides runsets and negative
  counts are invalid.
- `worktree changes` lists committed + uncommitted changes against the base commit.
- Gitlinks get their own submodule section; detached submodule HEADs are normal.

## 4. Start and configure an exploration

Write a falsifiable hypothesis: mechanism, workload, baseline, metric, threshold,
and constraints. Empty hypotheses warn but do not block creation.

```bash
featx node add <category>/<feature>/<sub> -p \
  -H "<measurable hypothesis>" --worktree --base <upstream-ref>

# Adopt an existing checkout; never assume main/master:
featx worktree create <node> --adopt \
  --path /abs/worktree --repo /abs/main-checkout \
  --base <root-ref> --sub-base TraceRTL=<submodule-ref>

featx recipe show <node> <build|run|post> --explain
featx recipe set <node> run W=20000000 I=40000000
featx recipe set <node> post -f post.yaml   # replaces the node layer
```

Recipe precedence: defaults → workload → root → ancestors → node → submit `--set`.

- Resolve base refs before registration and save full commit SHAs; do not invent a submodule base.
  Later branch movement does not change the base. Use `featx worktree set-base <node> <ref>
  [--sub TraceRTL]` only for explicit repair or migration of a legacy ref.
- Keep source unchanged during recording/compilation to avoid binary drift.
- Inline `key.path=value` merges; `null` deletes an inherited key; lists replace.
- Exec needs matching checkpoint/reference/restorer; trace needs matching trace
  and normal replay options. Do not use diagnostic replay shortcuts by default.
- Points are `<bench>_<cp>_<weight>`; coverage cannot combine with explicit points.
- Verify images, traces, weights, `W`, `I`, and comparison reference before submit.
- Node scripts inherit into `<runset>/scripts`; post steps must write `summary.json`.
- Same-workdir builds serialize at dispatch; different workdirs build in parallel.
  Cached emulators isolate running simulations from subsequent builds.

## 5. Submit and monitor

```bash
featx submit <node> build run post --dry-run
featx submit <node> build run post --purpose "<measurable purpose>"
featx ps
featx wait <task-id> [<task-id> ...] --timeout 3600
featx events --wait 600
```

- Dry-run resolves real recipes, presets, binaries, points, and commands without
  creating runsets, tasks, events, snapshots, links, or checkpoint caches.
- Record runset and build/run/post task IDs. Use `--bin`, `--after`, and
  `--priority` only as intended; running jobs are never preempted.
- `wait` returns `finished`, `blocked`, `pending`, and `timed_out`; only all
  requested tasks succeeded means success.
- `featx task show <task>` is a digest (counts, ETA, failed jobs); `--jobs --state failed`
  lists jobs. Avoid manual polling. Acknowledge events only after handling them.

## 6. Failures and iteration

```bash
featx logs <task-id> -j <failed-point> --err
featx task show <task-id>

# Design failure: document a real fix before relaunching unchanged inputs:
featx log append <runset> -p "<problem/evidence>" \
  -s "<fix/strategy>" -c "<code/configuration explanation>"
featx task relaunch <task-id>

# Changed code/scripts/recipes/binary/intent:
featx task resubmit <task-id> [--set kind.key=value] [--all-points]
```

- Infra failures retry automatically; design failures need diagnosis and a fix.
- Use CLI `--all-points` or MCP `only_failed=False` for full-suite verification.
- Dependency failures auto-cancel dependents; upstream relaunch revives only
  auto-cancelled tasks, never human-cancelled ones.

## 7. Results, provenance, and review

```bash
featx result <runset> --json
featx compare <reference-runset> <candidate-runset>
featx node set <node> --verdict inconclusive \
  --verdict-note "<evidence and limitations>" --evidence <node>/<runset>
```

- Check exact code/recipe/binary/inputs, completed points, drift, reference,
  PERF block, W, I, and point-set compatibility.
- `featx result` is a digest (tasks, `failures`, post metrics, review); `--provenance`,
  `--recipes`, `--overrides`, `--reports` add a section, `--full` adds all of them.
- Build records HEAD + patch in `<runset>/code`; large/build-output content may
  be omitted. Code views rebuild in temporary object directories without writes.
- Compare code with MCP `runset_code(runset, against=...)` or REST `/runsets/code`;
  there is no `featx runset code` CLI command.
- All-terminal runsets need review when verdict is `open` or evidence is empty.
  Failure and cancellation also count; they never set verdict/evidence automatically.
- Choose `supported`, `refuted`, or `inconclusive` from evidence. A non-open
  verdict plus evidence clears the CLI **Review needed** reminder on refresh.

## 8. MCP operation map

| Purpose | Tool |
|---|---|
| Tree / concise node | `tree_list`, `node_show` |
| Full node context | `node_context` or `node_show(full=True)` |
| Create/update/move node | `node_create`, `node_update`, `node_move` |
| Recipe/script | `recipe_get`, `recipe_set`, `script_read`, `script_write` |
| Worktree/base | `worktree_create`, `worktree_set_base` |
| Preview/submit | `task_submit(dry_run=True/False)` |
| Wait/status/log | `task_wait`, `task_status`, `task_log` |
| Relaunch/resubmit/cancel | `task_relaunch`, `task_resubmit`, `task_cancel` |
| Events | `events_wait`, `events_ack` |
| Results/review/code | `result_get`, `runsets_list`, `runset_code` |
| Archive/checkpoints | `runset_archive`, `checkpoints` |
| Baselines/storage/hosts | `baselines`, `storage_report`, `hosts` |
| Leaderboard/orphans/docs | `leaderboard`, `reap`, `docs` |

- `task_wait`: up to 86400 s; `events_wait`: up to 1800 s. Pending/blocked ≠ success.
- `node_create` returns warnings for a blank hypothesis.
- `node_move(dry_run=True, keep_branches=True)` previews without renaming branches.
- Read tool errors; do not retry unchanged failing calls.

## 9. Web UI and developing featx

```bash
featx ui
```

- The UI shares CLI/MCP state. EN / 中文 saves the browser preference; built-in
  labels/dialogs/hints localize, unknown labels fall back to English, and user
  content (paths, refs, commands, logs, recipe/metric keys, docs) stays unchanged.
  CLI localization is outside this scope.
- The UI hides review-needed badges, banners, and event rows; use the node's
  verdict/evidence editor instead.
- Code map: `cli.py` (CLI), `service.py` + `context.py` (shared operations),
  `worktree.py`, `sched/scheduler.py` + `sched/daemon.py`, `mcp_server.py`,
  `ckpt.py` + `points.py` + `presets.py` + `recipes.py`, web `web/src` + `i18n.tsx`
  (`web/dist` is generated, never edited manually).
- Add tests in `tests/`, keep APIs additive, and update README/reference/MCP/this
  guide. Keep dry-run/read-only operations side-effect-free; localize with `t()`.

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
cd featx/web && npm test && npm run build
.venv/bin/python -m featx.docs   # after plugin-manifest changes
```

Exercise changed workflows in an isolated root with real `featx-exec`. Do not
restart production `featxd` during testing; deployment is a separate decision.

## 10. Completion checklist

- Hypothesis and acceptance criteria are explicit.
- Actual code, recipes, binaries, and inputs are recorded.
- No relevant task is active, pending, blocked, or silently missing.
- Required backend/web tests and production builds pass on the final tree.
- Comparisons use compatible references and point sets; thresholds are interpreted correctly.
- Failures and limitations are stated; unverified work is not called done.
- Verdict, evidence, and notes reflect measured evidence, not just exit codes.
- Remaining review, commit, push, or deployment steps are distinguished from implementation.
