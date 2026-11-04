# AGENTS.md — featx agent rules

Use featx to preserve: **intent → code → experiments → evidence → conclusions**.
The feature tree is the source of truth; do not create untracked experiments in
ad-hoc directories. See `docs/reference.md` for full syntax and `intent.md` for
rationale. Keep this guide, both READMEs, and the reference in sync.

## 1. Mental model

- **Node**: hypothesis, verdict, evidence, notes, Git info, recipes, and runsets.
- **Worktree**: checkout associated with a node; make repository changes there.
- **Runset**: dated experiment `<node>/<YYYYMMDD-n>` with actual inputs and outputs.
- **Task**: build, run, or post, normally build → run → post.
- **Job / attempt**: scheduled work and each execution attempt.
- **Evidence**: runsets/artifacts supporting a conclusion; never inferred from exit status.

The daemon owns scheduling and process state. Jobs survive the interactive session.

## 2. Safety and compatibility

- Inspect node, worktree, tasks, Git status, and runsets before mutating anything.
- Preserve user files and active experiments; no unauthorized reset/clean/rebase/
  force-push/cancel/delete/gc/restart.
- **Match CLI and daemon versions.** Never send `submit --dry-run` to an old
  daemon: it may ignore the flag and perform a real submission.
- Use temporary `FEATX_ROOT` and repositories for tests, not production state.
- Do not expose tokens or read secret files such as `.env`.
- `reap --apply`, `gc --apply`, `node rm`, `task cancel`, and `daemon stop`
  require explicit authorization. Dry-run and inspect first.
- Never claim success from missing, pending, blocked, or unverified results.

## 3. Resume or inspect an exploration

```bash
featx daemon status
featx config show effective
featx ps
featx tree
featx node show <node> [--full --json]
featx worktree status <node>
featx worktree changes <node> [--json]
featx result <node>/<YYYYMMDD-n> [--json]
```

- `node show`: concise research record; `--full` YAML, `--full --json` full JSON.
- `--runsets 0` hides runsets; negative counts are invalid.
- `worktree changes`: committed + uncommitted changes vs each merge-base.
- Gitlinks appear in their own submodule section; detached submodule HEADs are normal.

## 4. Start an exploration

Write a falsifiable hypothesis: mechanism, workload, baseline, metric, threshold,
and constraints. Empty hypotheses warn but do not block creation.

```bash
featx node add <category>/<feature>/<sub> -p \
  -H "<measurable hypothesis>" --worktree --base <upstream-ref>

# Adopt an existing checkout; never assume main/master:
featx worktree create <node> --adopt \
  --path /abs/worktree --repo /abs/main-checkout \
  --base <root-ref> --sub-base TraceRTL=<submodule-ref>

featx worktree set-base <node> <ref> [--sub TraceRTL]
```

- Validate base refs before registration; do not invent a submodule base.
- Keep source unchanged during recording/compilation to avoid binary drift.
- Same-workdir builds serialize at dispatch; different workdirs build in parallel.
- Cached emulators isolate running simulations from subsequent builds.

## 5. Configure an experiment

Recipe precedence: defaults → workload → root → ancestors → node → submit `--set`.

```bash
featx recipe show <node> build --explain
featx recipe show <node> run --explain
featx recipe show <node> post --explain
featx recipe set <node> run W=20000000 I=40000000
featx recipe set <node> post -f post.yaml   # replaces the node layer
```

- Inline `key.path=value` merges; `null` deletes an inherited key; lists replace.
- Exec needs matching checkpoint/reference/restorer; trace needs matching trace
  and normal replay options. Do not use diagnostic replay shortcuts by default.
- Points are `<bench>_<cp>_<weight>`; coverage cannot combine with explicit points.
- Verify images, traces, weights, `W`, `I`, and comparison reference before submit.
- Node scripts inherit into `<runset>/scripts`; post steps must write `summary.json`.

## 6. Preview, submit, and monitor

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
- `wait` returns `finished`, `blocked`, `pending`, and `timed_out`.
  Only all requested tasks succeeded means success; timeout/blocked is not success.
- Avoid manual polling. Acknowledge events only after handling them.

## 7. Failure handling and iteration

```bash
featx logs <task-id> -j <failed-point> --err
featx task show <task-id>

# Design failure: document a real fix before relaunching unchanged inputs:
featx log append <runset> -p "<problem/evidence>" \
  -s "<fix/strategy>" -c "<code/configuration explanation>"
featx task relaunch <task-id>

# Changed code/scripts/recipes/binary/intent:
featx task resubmit <task-id> [--set kind.key=value]
featx task resubmit <task-id> --all-points
```

- Infra failures retry automatically; design failures need diagnosis and a fix.
- Use CLI `--all-points` or MCP `only_failed=False` for full-suite verification.
- Dependency failures auto-cancel dependents; upstream relaunch revives only
  auto-cancelled tasks, never human-cancelled ones.
- Use `featx reap` to inspect stale processes; never kill by PID/name alone.

## 8. Results, provenance, and review

```bash
featx result <runset> --json
featx compare <reference-runset> <candidate-runset>
featx node set <node> --verdict inconclusive \
  --verdict-note "<evidence and limitations>" --evidence <node>/<runset>
```

- Check exact code/recipe/binary/inputs, completed points, drift, reference,
  PERF block, W, I, and point-set compatibility.
- Build records HEAD + patch in `<runset>/code`; large/build-output content may
  be omitted. Code views rebuild in temporary object directories without writes.
- Use MCP `runset_code(runset, against=...)` or REST `/runsets/code` for code
  comparison; there is no `featx runset code` CLI command.
- All-terminal runsets need review when verdict is `open` or evidence is empty.
  Failure and cancellation also count; they never set verdict/evidence automatically.
- Choose `supported`, `refuted`, or `inconclusive` from evidence. A non-open
  verdict plus evidence clears the CLI **Review needed** reminder on refresh.

## 9. MCP operation map

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

## 10. Web UI and language

```bash
featx ui
```

- The UI shares CLI/MCP state. EN / 中文 saves the browser preference.
- Built-in labels/dialogs/hints are localized; unknown labels fall back to English.
- User content, Git refs, commands, paths, logs, recipe/metric keys, and reference
  documentation stay unchanged. CLI localization is outside this scope.
- The Web UI hides review-needed badges, banners and event rows; use the node's
  verdict/evidence editor.

## 11. Developing featx itself

- CLI: `cli.py`; shared operations: `service.py`, `context.py`; worktrees: `worktree.py`.
- Scheduler: `sched/scheduler.py`, `sched/daemon.py`; MCP: `mcp_server.py`.
- Input resolution: `ckpt.py`, `points.py`, `presets.py`, `recipes.py`.
- Web: `web/src`, `i18n.tsx`; `web/dist` is generated, never edited manually.
- Add tests in `tests/`, keep APIs additive, and update README/reference/MCP/this guide.
- Keep dry-run/read-only operations side-effect-free. Localize built-in labels with `t()`.

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
cd featx/web && npm test && npm run build
.venv/bin/python -m featx.docs   # after plugin-manifest changes
```

Exercise changed workflows in an isolated root with real `featx-exec`. Do not
restart production `featxd` during testing; deployment is a separate decision.

## 12. Completion checklist

- Hypothesis and acceptance criteria are explicit.
- Actual code, recipes, binaries, and inputs are recorded.
- No relevant task is active, pending, blocked, or silently missing.
- Required backend/web tests and production builds pass on the final tree.
- Comparisons use compatible references and point sets; thresholds are interpreted correctly.
- Failures and limitations are stated; unverified work is not called done.
- Verdict, evidence, and notes reflect measured evidence, not just exit codes.
- Remaining review, commit, push, or deployment steps are distinguished from implementation.
