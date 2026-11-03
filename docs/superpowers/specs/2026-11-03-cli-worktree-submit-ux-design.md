# Featx worktree and CLI usability improvements

## Goal

Implement the five approved usability improvements without changing featx's
experiment, runset, or binary-provenance model:

1. Validate and expose worktree and submodule bases.
2. Clarify status labels and add `worktree changes`.
3. Queue same-worktree builds, enforcing exclusion at dispatch time.
4. Add side-effect-free submit dry-run and help aliases.
5. Make `node show` concise by default, with full and JSON modes and the
   hypothesis, verdict, evidence, and comment retained.

The existing full node context remains available to MCP and REST clients.
Existing JSON fields remain compatible; new fields are additive. No unrelated
untracked files in the repository are modified.

## 1. Worktree bases and changes

At create/adoption, resolve each configured base with
`git rev-parse --verify <base>^{commit}` in the repository that will use it.
Reject an invalid base before creating or registering worktrees. Do not guess a
base or silently retain an unresolved default.

Add `--sub-base NAME=REF` to `worktree create`, including daemon forwarding.
Add `worktree set-base NODE REF [--sub NAME]` to repair either the superproject
base or one registered submodule base. Validate the new ref before saving the
node metadata. The existing adopted-worktree ownership rules do not change.

Extend worktree status with each repository's base and computed merge-base,
including registered submodules. Existing nodes with bad legacy metadata show a
specific error and can be repaired with `set-base`.

Add `featx worktree changes NODE [--json]`, backed by the existing
`service.worktree_changes`. The default prints per-repository merge-base,
ahead/behind counts, changed files and line totals; JSON returns the complete
existing structure. Submodule gitlinks remain excluded from the superproject
and are reported in the submodule's own section.

Update the web worktree labels to say “No uncommitted changes,” “Committed +
working-tree changes vs <base>,” and “Detached at pinned commit <sha>” where
applicable. Explain that gitlinks are shown separately. Preserve API field names
such as `dirty` and `branch`; only presentation changes.

## 2. Dispatch-time build exclusion

Remove the submit-time rejection for another active build with the same
workdir. Accept dependent and independent submissions normally.

At dispatch, compare canonical (`Path.resolve()`) workdirs under the scheduler
lock. A queued build whose workdir belongs to another materialized/running build
stays queued with a visible `waiting for build workdir (<task-id>)` note. Once
the active build reaches a terminal state, the queued build materializes in
priority/FIFO order. A bincache hit completes without occupying a compilation
slot. Dependency failure and cancellation behavior is unchanged.

Keep a defensive check at job reservation so legacy or unexpected state cannot
launch two build jobs against one workdir. The check and state transition are
atomic under the scheduler lock. Different workdirs can still build in
parallel. Running simulations are unaffected because they use cached emulators.

## 3. Submit dry-run and help aliases

Add `featx submit ... --dry-run`, with daemon and MCP support. Reuse the real
recipe substitution, preset lowering, binary-mode resolution, point validation,
and task-dependency construction. Separate those operations from persistence:
`recipes.snapshot_scripts`, runset creation, binary symlinks, provenance patch
saving, task stores, and event emission happen only for a real submit.

Preview uses an explicitly marked preview runset name/path and returns the
resolved task specs, dependencies, selected points and input paths, rendered
point commands, source/submodule revisions, bincache information that is
computable read-only, resource requirements, and nonfatal workdir-wait warnings.
It creates no runset, task IDs in persistent storage, directories, snapshots,
symlinks, or events. Preview-only IDs and paths are marked as such.

Add documentation aliases `submit -> launch`, `worktree -> worktrees`,
`runset -> runsets`, `recipe -> recipes`, and `ckpt -> checkpoints`. Canonical
reference topics and plugin help continue to work.

## 4. Concise node show

Leave `service.node_context` and the MCP full-context behavior intact. Add a
brief service view containing:

- node path, status, hypothesis, verdict, verdict note, evidence, comment, and
  tags;
- worktree/branch/base/head and uncommitted-change summary;
- the requested latest runsets with purpose, task states, and non-null headline
  post metrics;
- runset/archive counts.

Default `featx node show` prints this brief view as text. `--json` prints the
same brief view as JSON. `--full` selects the existing complete context;
without `--json` it is emitted as readable YAML, with `--full --json` it is the
existing full JSON structure. This makes verbosity and output format explicit.

Evidence remains a list of references rather than expanded runsets. Long text
is not silently dropped. `--runsets 0` returns no runsets; negative counts are
rejected. Existing callers of `service.node_context` are unchanged.

## Tests and verification

Add focused tests to the existing suite:

- Temporary Git repositories: invalid base rejection before side effects,
  submodule base validation/repair, status merge-bases, and changes totals.
- CLI/API: worktree changes, help aliases, concise/full/JSON node output,
  research fields, and `--runsets 0`.
- Scheduler: two same-worktree submissions are accepted, only one build job is
  active at once, the second starts after the first, different workdirs run in
  parallel, cache hits do not block, and dependency failure/cancellation and
  daemon recovery preserve exclusion.
- Dry-run: output matches real submission resolution while the data root,
  task store, runset tree, and event inbox remain unchanged.

Run `.venv/bin/pytest -q`, the existing web tests/build if web labels change,
`featx help submit`, `featx worktree changes`, `featx submit --dry-run` against
an isolated fixture, and read-only checks against the real regression node.
Do not restart the production daemon or alter running production tasks as part
of verification; deployment is a separate step.

Update `docs/reference.md`, command help, and the web bundle according to the
repository's existing conventions. Run the complete test suite after all five
changes and report any pre-existing failures separately from regressions.
