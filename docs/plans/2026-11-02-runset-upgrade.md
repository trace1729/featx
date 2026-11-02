# featx: richer runsets, task-state filters, dependency-failure cleanup (2026-11-02)

## Context
At the moment a runset is little more than a working environment. You asked for four things:
- **Code per runset.** Each runset in a node is one iteration of the node's idea, and it should show the code it ran. That code should be comparable against the node base, another runset, or the worktree as it is now.
- **Parameters per stage.** Show the important build/run/post parameters of each runset.
- **Archive (hide) runsets.**
- **UI and task cleanup.**
  - Fold untracked files in the code view.
  - Filter tasks by state.
  - Clean up tasks whose dependency failed, in case they leave orphan processes.

What the code does today:
- **Code recording.** At submit, the runset records HEAD, a diff hash and a tracked-only `src-patches/` copy (`provenance.worktree_state`/`save_patches`). The build compiles the live worktree later. The bincache key is computed at materialize time (`Scheduler._build_key`), so the source can drift between submit, keying and compile.
- **Untracked files and overrides.** Untracked file contents are never saved, and the raw `--set` overrides are lost.
- **Recipe snapshots.** The resolved recipe snapshots exist (`Runset.recipes`, in `experiment.yaml`), but no API or UI shows them.
- **Archiving.** There is no runset-level archive.
- **Untracked list.** The worktree view lists every untracked file as its own row (`Node.tsx RepoChangesView`). The list is cut silently at 2000.
- **Task list filters.** The Tasks page and `/tasks` can filter only by node text and the finished toggle. There is no state filter.
- **Dependency failures.**
  - Downstream tasks stay `queued` forever with the note "blocked: a dependency failed" (`_tick_locked`). Production has 12 of them now, from 10-26 to 10-31.
  - I checked all 24 hosts and found no featx process, so these tasks are not orphan processes. They are stuck queue entries. They clutter `ps`, block `node mv`, pin binaries against gc and keep the runset unsealed.
  - The cancel cascade is not transitive.
  - The revive code in `relaunch()` never fires: it looks for `failed && !jobs`, a state a dependency failure never produces.
- **Real orphan gaps**, in other places:
  - (a) A launch that is reported failed (ssh timeout) but starts later is never killed.
  - (b) The same applies to the "never started" grace path.
  - (c) A kill sent to an unreachable host is tried only once.
  - (d) The `_orphans` and `_kills` lists live in memory and are lost on a daemon restart.
  - (e) Running jobs of a `blocked` task are not polled, but its queued jobs still launch.

Decisions you made: git snapshot refs for code; auto-cancel tasks whose dependency failed and revive them on relaunch; commit your `zh` work-in-progress first and branch from it.

## 0. Setup
1. Run pytest in `toolsets/featx` on `zh`, then commit the work-in-progress as two commits. The `research_directions*` files and `.pdfvenv/` stay untracked.
   - `feat(build): pipeline tracertl (TraceRTL's own make entry)`, plus the `--set` null fix in `cli._overrides`. Files: presets, jobs, scheduler, build.yaml, cli, docs and their tests.
   - `feat(post): perf-keys rms/IPC MAE/RMSE; analysis scripts tolerate missing BPU training counters`.
2. Create the worktree `toolsets/featx-runset1102`, branch `featx-runset1102`, from `zh` with `util/add-worktree-with-shared-submodules.sh`.
3. Each problem I hit goes into `problem-analysis.log`, in the required Chinese format.
4. Copy this plan to `docs/plans/2026-11-02-runset-upgrade.md` in the new worktree and commit it with the first commit.

## 1. Tasks: dependency failures and orphan processes (`featx/sched/scheduler.py`, `model.py`, `waiting.py`)
1. **Auto-cancel tasks whose dependency failed.**
   - When `_deps_state(task) == "broken"` in `_tick_locked`, set `state=cancelled`, `finished`, the note `dependency <kind> <id12> failed`, and a new field `Task.cancelled_by: str | None` (the upstream id). Then emit `cancelled` with `reason: dependency`.
   - The cascade is transitive on its own: the next tick sees the cancelled task as broken for its own dependents.
2. **Make `cancel()` transitive.** Walk every queued dependent, not just the direct ones. They get `cancelled_by=<root>`.
3. **Revive on relaunch.** Replace the dead revive block in `relaunch()` with a transitive walk.
   - Revive only dependents with `cancelled_by` set and no jobs; set them back to `queued` and clear the field.
   - Dependents you cancelled yourself keep their state.
4. **`waiting.py`.** A cancelled task with `cancelled_by` is reported under `blocked`, together with its `failed_dependencies`. So `featx wait` and MCP `task_wait` behave as they do today, including the exit code.
5. **Orphan hardening.**
   - (a)/(b) When a launch fails or never starts, register `(host, jdir, attempt)` in `_orphans` with a long deadline (`liveness.orphan_watch`, 900 s). A late start is then killed.
   - (c)/(d) Persist `_kills` and `_orphans` to `$ROOT/sched-pending.json`. Kills to unreachable hosts are retried every tick until `liveness.kill_retry_hours` (24 h) runs out.
   - (e) `_reserve_launches` skips jobs of tasks with `blocked=True`. `_tick_locked` still polls their running jobs.
6. **`featx reap [--apply]`**, also `POST /reap` and the MCP tool `reap`.
   - Over one ssh per host, list this user's `featx-exec <jdir> <attempt>` processes. Keep the ones whose jdir is under this root, and flag the ones whose job is not active or whose attempt is not current.
   - It is a dry run by default. `--apply` kills the whole session with `executor.kill_many`.
   - The daemon also runs the scan at start and every `liveness.reap_interval` (600 s). It kills only confirmed orphans and emits an `orphan` event for each.
7. **List filter.** `GET /tasks?state=a,b`, where `blocked` means `cancelled_by` set or `task.blocked`; `featx ps --state failed,blocked`; MCP `task_status(states=...)`.

## 2. Runset parameters and archive (`model.py`, `context.py`, `service.py`, `daemon.py`, `cli.py`, `mcp_server.py`)
1. **Record overrides.** New field `Runset.overrides: dict` holds the raw submit overrides, set in `Scheduler.submit` and `resubmit`.
2. **New module `featx/params.py`.**
   - `stage_params(recipes, bin)` returns headline keys per stage:
     - build: tool, mode, pipeline, the non-default options, the bincache key and whether it hit the cache;
     - run: mode, workload or suite, checkpoint, W, I, the point count, benchmarks, coverage, env;
     - post: each step with its plugin, baselines and non-default `with` values.
   - `differing(runsets)` flattens the recipe snapshots of a node's runsets and returns the leaf keys whose values differ. Volatile keys are ignored: runset dirs, `${scripts}` paths, timestamps.
   - The table shows headline keys plus differing keys, so the reason each runset differs stands out without reading YAML.
3. **Exposure.**
   - `runset_summary` gains `params`, `overrides` and `archived`.
   - `result_get` gains `recipes`, `overrides` and `params`.
   - `featx result` prints a params block.
4. **Archive.**
   - New fields `Runset.archived: bool`, `archived_at` and `archive_note`, declared in the model; pydantic would drop undeclared keys.
   - `POST /runsets/archive {ref, archived, note}`, `featx runset archive REF [--undo] [-m NOTE]` and the MCP tool `runset_archive`. It is refused while the runset has active tasks.
   - Archived runsets are hidden by default from:
     - the node runsets list and `node_context` (`archived_count` is reported, with an `include_archived` flag);
     - `tree_list`'s latest runset;
     - the leaderboard (an `include_archived` option shows them).
   - Data and bins are kept, and gc does not change. Evidence that points to an archived runset shows an "archived" badge.

## 3. Code snapshots (`featx/provenance.py`, new `featx/snapshots.py`, `scheduler.py`, `worktree.py`)
1. **`snapshot(repo_path, ref_name)`.** It builds a commit without touching the user's index or worktree:
   - fill a temporary index from HEAD (`GIT_INDEX_FILE` + `read-tree HEAD`);
   - add tracked changes with `add -u`;
   - add untracked files that are not ignored, reusing the caps in `provenance` (`MAX_UNTRACKED_BYTES` 8 MiB per file, 2000 files); larger or extra files are listed in `skipped`;
   - `write-tree`, then `commit-tree -p HEAD`;
   - `update-ref refs/featx/runsets/<runset_id>`.
   A clean tree points the ref at HEAD. The ref keeps commits alive across rebases, and no branch is created.
2. **Scope.** The superproject and every submodule that `worktree_state` covers, the dedicated ones and the shared/symlinked ones alike; this is what the build compiled. Shared ones are flagged `shared`.
3. **When.**
   - **At build materialize** (`_build_key`), using the same state as the key. `rs.provenance.snapshot` is set to `{super: {commit, head, branch, base, merge_base, untracked: [...], skipped: [...]}, submodules: {...}}`.
   - **At build launch** (`_prepare_job` for a build job), recompute `worktree_state`. If it differs from the keyed state, re-snapshot and re-key, and record `drift: "after submit"`.
   - **At build finish**, compare once more. If the tree changed during compile, set `drift: "during build"` and **do not** store the binary in bincache, because it would sit under a key that doesn't match its source. The runset still uses the binary.
4. **Runsets without a build.**
   - With `--bin <key|runset>`, the bincache manifest says which runset built it, so the code view says "code from runset X" and links to it.
   - An imported `ext-` binary has no code view.
   - Legacy runsets show `provenance.head` plus their `src-patches/*.patch`.
5. **API.**
   - `GET /runsets/code?ref=R&against=base|prev|worktree|<runset ref>` returns the same shape as `worktree.changes`: per repo `files`, `commits`, `untracked`, `total`, plus `snapshot`, `drift` and `source_runset`.
   - `GET /runsets/code/diff?ref&against&sub&path` returns one file's diff.
   - Implementation: refactor `worktree._repo_changes` into a `_diff_shape(repo, a, b|None)` helper, shared with the live worktree view. `b=None` means the working tree.
   - `against=worktree` diffs the snapshot against the live tree, including untracked files.
   - `prev` is the closest earlier runset of the node that has a snapshot.
6. **Ref hygiene.** `node rm --drop-worktrees` deletes that node's `refs/featx/runsets/*`. The refs are not pushed by a normal `git push`; the docs note that `--mirror` would push them.

## 4. Web UI (`featx/web/src`)
1. **Runset page** (`pages/Runset.tsx`).
   - A **Parameters** card under the header: three columns build/run/post, with the headline keys, the `--set` overrides marked, and a "full recipe" YAML toggle.
   - A new **code** tab with an against selector: node base, previous runset, worktree now, or pick a runset. It shows the snapshot id, a drift warning, and the "code from runset X" link.
   - The code tab reuses a generalized `RepoChangesView` (moved to `components/Changes.tsx` with an injectable diff fetcher).
   - A job **state filter** above `JobsTable`: chips with counts. The selection is kept in the URL (`?jobs=failed`).
   - **Archive/unarchive** button.
2. **Node page** (`pages/Node.tsx`).
   - The runsets table gets a params column (the differing keys) and a code column (snapshot id, files and +/− vs base).
   - Select two runsets and click **compare**, which opens the code tab with `against=`.
   - A "show archived (N)" toggle.
3. **Folding untracked files**, in the worktree tab and the code tab.
   - The untracked section is collapsed by default: "untracked: N files in K dirs".
   - When expanded, files are grouped into folded directory rows with counts. Single-child chains are collapsed, and a click on a dir expands it.
   - The server returns `untracked_total` and `untracked_truncated`, so a truncated list is labelled.
4. **Tasks page** (`pages/Tasks.tsx`).
   - State filter chips with counts: queued, running, blocked, succeeded, failed, cancelled (multi-select).
   - Saved with `usePersistentState("tasks:states")`, next to the existing text filter.
5. i18n entries (zh) for every new label. Rebuild and commit `featx/web/dist`.

## 5. Docs
README (Node, Runset and Tasks pages, archive, reap, dependency-failure behaviour, snapshots), `featx/docs/reference.md` (topics `concepts` and `runsets`, the new MCP tools), and the memory note.

## Backward compatibility
- **Stored files.** Existing `experiment.yaml` and `tasks/*.json` load unchanged. Every new field has a default: `overrides={}`, `archived=False`, `cancelled_by=None`. Old runsets show "overrides not recorded".
- **Rolling back to the old code** keeps working, but the old models drop the new fields the next time they save a file (pydantic `extra="ignore"`). An archive flag can therefore come back unarchived.
- **Bincache.** The key formula is unchanged, so every existing binary still hits the cache. Snapshots are recorded beside the key and are not part of it. Drift handling only changes builds whose tree changed after keying.
- **Parameters** come from the `Runset.recipes` snapshots that every existing runset already has. Legacy `type: gsim-two-step` recipes are handled. This is checked read-only against all production runsets.
- **Code view of old runsets.** Old runsets have no snapshot. They show the recorded HEAD plus `src-patches`, which hold tracked changes taken at submit; their untracked files were never saved. There is no backfill (your choice), and no refs are written for old runsets.
- **Leaderboard and comparability class:** unchanged. `archived=False` by default.
- **APIs** only gain fields, endpoints and optional parameters, so existing CLI, MCP and REST calls behave the same.
  - The new UI bundle needs the new daemon (new endpoints), so the merge and the restart go together.
- **Jobs running across the restart** are re-adopted as today; `featx-exec` is unchanged.
- **The one change of old state** (you approved it): the 12 tasks stuck in `queued` because their dependency failed become `cancelled (dependency … failed)` on the first tick. A relaunch of the upstream task revives them, as it does for new tasks. Tasks you cancelled yourself are left alone.
- `src-patches/` keeps being written, so tools that read it still work.

## Verification
1. **pytest**, new tests:
   - dependency auto-cancel, transitive cancel, revive on relaunch, and that a user-cancelled task is not revived;
   - `waiting` reports dependency-cancelled tasks as blocked;
   - a late-starting launch is killed;
   - pending kills survive a scheduler restart;
   - jobs of a blocked task are polled but not launched;
   - `reap` on localhost against a planted stale `featx-exec`;
   - snapshot on a temporary git repo with a submodule, an untracked file and a 9 MiB file: ref created, the big file skipped, the index untouched;
   - code API against base, prev, worktree and another runset;
   - drift handling: re-key at launch, no bincache store when the tree changes during the build;
   - params and differing keys;
   - archive filtering and the refusal while tasks are active;
   - `/tasks?state=`.
2. **vitest:** untracked folding, the task state filter, the jobs filter, the parameters card, the code tab and compare, the archive toggle. Then `tsc` plus the build.
3. **Read-only checks on production data.**
   - Run `params.stage_params` and `differing` over every existing `experiment.yaml` under `/nfs/share/gkc/featx/tree`; none may error.
   - Check that the legacy code view renders for the runsets that have `src-patches`.
4. **End-to-end on a dev daemon**, port 8766 with its own root, a fake emu, and a temporary git repo as the node worktree:
   - two runsets with different edits: compare them in the UI and against the worktree;
   - archive one;
   - a failing build leaves its run and post cancelled with the note "dependency …", and a relaunch revives them;
   - Playwright screenshots of the Runset code and Parameters views, the Node table and the Tasks filters.
5. **Merge and restart.** Fast-forward `zh` (the production branch). You restart featxd.
   - On its first tick the 12 stuck production tasks become `cancelled (dependency … failed)`. This is expected, and a relaunch of their upstream task revives them.
   - Snapshots start with the next build; existing runsets keep the legacy view.
