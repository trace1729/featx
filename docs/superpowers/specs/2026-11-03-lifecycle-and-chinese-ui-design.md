# Lifecycle reminders and Chinese web UI

## Goal

Add a non-destructive hypothesis → experiment → review reminder workflow and
complete built-in Chinese labels in the web UI. Chinese localization does not
cover the CLI, reference documentation, logs, recipes, or user-authored content.
The current partial CLI translation is outside this change's scope.

The implementation must never infer a verdict or add evidence from task exit
status. English web behavior and existing machine-readable structures remain
compatible; new fields and the new event type are additive.

## 1. Lifecycle reminders

### Node creation

Keep hypothesis optional. The web add-node dialog shows a visible recommendation
when it is empty, without disabling creation. `featx node add` prints the same
warning to stderr but still creates the node. Existing API and automation
behavior is preserved.

### Runset completion and review state

Compute review state from two independent facts:

- every task in the runset has reached a terminal state (`succeeded`, `failed`,
  or `cancelled`); and
- the node's verdict is `open` or its evidence list is empty.

A completed runset in this state is `review_needed`. Success is not required:
failed and cancelled experiments can provide evidence too. Existing sealed and
archive semantics do not change.

When a runset transitions to all-terminal and review is needed, emit one
`review_needed` event with the runset ref, outcome, node verdict/evidence, and a
suggested `featx node set` command. Do not emit it for runsets that already have
a non-open verdict and evidence, and do not emit it during scheduler recovery or
read-only listing. No task outcome changes the node's research fields.

Expose the computed review state in runset summaries and `featx result`. The
result CLI prints the reminder and suggested command without changing its
machine-readable summary. Existing `runset_done` behavior remains; the new
review event covers all terminal outcomes.

### Web review workflow

Show a `Review needed` badge/banner on all-terminal runsets requiring review.
Link it to the node's existing verdict/evidence editor so the user can choose a
verdict and attach the completed runset. The badge is computed from current node
state, so completing the research record clears it on refresh. No automatic
verdict or evidence mutation occurs in the UI.

## 2. Chinese web UI

Extend the existing `t()` function with optional placeholder values. A missing
Chinese entry falls back to the English template, after which `{name}`-style
placeholders are interpolated. Dynamic messages use complete templates rather
than concatenated translated fragments.

Cover built-in web UI text across pages, dialogs, buttons, badges, form labels,
validation messages, table headings, hints, and the new lifecycle reminders.
Keep terminology consistent in the central dictionary. Do not translate:

- user-authored hypotheses, comments, notes, or other content;
- Git refs, commands, paths, hostnames, trace/checkpoint names, and logs;
- recipe keys, metric keys, API/JSON keys, and reference documentation.

Do not add an i18n dependency. Preserve the existing language preference in
`localStorage`, English behavior, and fallback for unknown strings.

Add static checks that translation keys used by the UI have Chinese entries,
and exercise representative pages and the lifecycle workflow with Chinese
selected. Dynamic templates are checked for missing placeholders.

## 3. Compatibility and documentation

The new `review_needed` event and summary/result review fields are additive.
Existing `runset_done`, sealing, archive, and task behavior is unchanged.
Machine-readable JSON keys and English text stay stable. The production daemon
is not restarted during implementation or verification; deployment is separate.

Update `docs/reference.md`, both READMEs where relevant, and the generated web
bundle. Leave unrelated untracked files and the unrelated `.gitignore` edit
unchanged.

## 4. Verification

Add tests for:

- empty-hypothesis warning that does not block creation;
- review state for successful, failed, cancelled, active, and already-reviewed
  runsets;
- exactly one review event on the all-terminal transition and none during
  recovery or for an already-reviewed node;
- no automatic verdict/evidence mutation;
- result and web review workflows;
- Chinese dictionary coverage, placeholder interpolation, and representative
  Chinese pages with English fallback.

Run `.venv/bin/pytest -q`, web Vitest, TypeScript, and the production web build.
Exercise the real regression node read-only and confirm its review suggestion
points to its two completed runsets without modifying them.
