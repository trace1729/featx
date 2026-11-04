import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import YAML from "yaml";
import { api } from "../api";
import { AddNodeDialog, MoveDialog, RemoveDialog } from "../components/NodeDialogs";
import { ChangesView } from "../components/Changes";
import { Markdown } from "../components/FileViewer";
import { Badge, CopyTitle, Dialog, ErrorBox, Loading, StateBadge, Tabs, VerdictMark, useAction } from "../components/ui";
import { YamlEditor } from "../components/YamlEditor";
import type { Changes, CheckpointSet, ConfigView, NodeContext, RunsetSummary } from "../types";
import { NO_PRESETS, checkpointGroups, coveredByTokens, deepMerge, paramTokens, sharedTokens, shortCommit, fmtTime, fmtValue, loadStored, metricLabel, parseCsv, parseOverrides, presetOverrides, saveStored, setFlags, usePersistentState } from "../util";
import type { QuickPresets } from "../util";
import { useI18n } from "../i18n";

const VERDICTS = ["open", "supported", "refuted", "inconclusive"];
const STATUSES = ["active", "parked", "done", "abandoned"];
const KINDS = ["build", "run", "post"] as const;
const TABS = ["runsets", "submit", "worktree", "notes"] as const;
type Tab = (typeof TABS)[number];
// recipes and scripts live inside the submit tab now; old ?tab= links keep working
const NODE_TAB_ALIASES: Record<string, Tab> = { recipes: "submit", scripts: "submit", settings: "runsets" };

function useInvalidateNode() {
  const qc = useQueryClient();
  return () => Promise.all([qc.invalidateQueries({ queryKey: ["node"] }), qc.invalidateQueries({ queryKey: ["tree"] })]);
}

/** The few numbers worth a column: every post step's self.* error/ipc keys. */
function keyMetrics(rs: RunsetSummary): [string, unknown][] {
  const out: [string, unknown][] = [];
  for (const [step, s] of Object.entries(rs.post ?? {})) {
    for (const k of ["self.mean_abs_err_pct", "self.ipc_geomean"]) {
      if (k in s) out.push([`${step}.${k}`, s[k]]);
    }
  }
  return out;
}

const MAX_PARAM_TOKENS = 4;

/** Only what differs between the shown runsets, on one line (stage letter, then the tokens);
 * the full headline parameters and the other differing recipe keys are in the tooltip. A node
 * with a single runset has nothing to compare: the cell is "—" with the tooltip. */
function ParamsCell({ rs, shared, extra, single }: { rs: RunsetSummary; shared: Set<string>; extra: string[]; single: boolean }) {
  const { t } = useI18n();
  const tokens = paramTokens(rs.params, rs.code);
  const values = rs.differing_values ?? {};
  const mine = single ? [] : extra.filter((k) => k in values);
  const full = KINDS.filter((stage) => tokens[stage].length).map((stage) => `${stage}: ${tokens[stage].map((x) => x.text).join(" · ")}`);
  if (mine.length) full.push("", ...mine.map((k) => `${k} = ${fmtValue(values[k])}`));
  const diff = single ? [] : KINDS.flatMap((stage) => tokens[stage].filter((x) => !shared.has(`${stage}.${x.key}`)).map((x) => ({ stage, ...x })));
  const shown = diff.slice(0, MAX_PARAM_TOKENS);
  const more = diff.length - shown.length + mine.length;
  const title = full.join("\n") || undefined;
  if (!shown.length && !more) return <span className="muted small" title={title}>{single ? "—" : t("same")}</span>;
  return (
    <span className="params-cell small mono" title={title}>
      {shown.map((x, i) => (
        <Fragment key={`${x.stage}.${x.key}`}>
          {i > 0 && <span className="muted"> · </span>}
          {(i === 0 || shown[i - 1].stage !== x.stage) && <span className="params-stage">{x.stage[0]}</span>}
          <span className="params-diff">{x.text.length > 18 ? `${x.text.slice(0, 16)}…` : x.text}</span>
        </Fragment>
      ))}
      {more > 0 && <span className="muted"> +{more}</span>}
    </span>
  );
}

/** The latest task of each stage; tasks a later submit into the same runset superseded are only counted. */
function TasksCell({ tasks }: { tasks: RunsetSummary["tasks"] }) {
  const { t } = useI18n();
  const latest = new Map<string, RunsetSummary["tasks"][number]>();
  for (const task of tasks) latest.set(task.kind, task);
  const earlier = tasks.filter((task) => latest.get(task.kind) !== task);
  return (
    <>
      {[...latest.values()].map((task) => (
        <span key={task.id} style={{ marginRight: 6, whiteSpace: "nowrap", display: "inline-block" }}>
          {t(task.kind)}: <StateBadge state={task.state} />
        </span>
      ))}
      {earlier.length > 0 && (
        <div className="muted small" title={earlier.map((task) => `${task.kind} ${task.id.slice(0, 12)}: ${task.state}`).join("\n")}>
          +{earlier.length} {t("earlier tasks")}
        </div>
      )}
    </>
  );
}

/** The HEAD the build compiled and, labelled, the uncommitted changes it saved (from the
 * summary, no code query); a run-only runset links the runset that built its binary. */
function CodeCell({ rs }: { rs: RunsetSummary }) {
  const { t } = useI18n();
  const code = rs.code;
  // a build of its own recorded the code; otherwise a run-only runset's code is its binary's
  if (code?.from && !(code.head && !code.legacy))
    return (
      <span className="small muted" style={{ whiteSpace: "nowrap" }} title={t("binary built by {ref}", { ref: code.from })}>
        ← <Link to={`/runset/${code.from}?tab=code`}>{code.from.split("/").pop()}</Link>
      </span>
    );
  if (!code?.head) return <span className="muted small">—</span>;
  const u = code.uncommitted;
  const title = [
    `HEAD ${code.head}${code.branch ? ` (${code.branch})` : ""}`,
    u ? t("uncommitted: {files} files +{added} −{deleted} in {repos}", { files: u.files, added: u.added, deleted: u.deleted, repos: u.repos.map((r) => r || t("superproject")).join(", ") }) : code.dirty ? t("uncommitted changes (not saved)") : t("no uncommitted changes"),
    code.omitted ? t("{count} untracked entries not saved", { count: code.omitted }) : "",
    code.legacy ? t("legacy runset: recorded at submit") : "",
  ].filter(Boolean).join("\n");
  return (
    <span className="mono small" style={{ whiteSpace: "nowrap" }} title={title}>
      <span className={code.legacy ? "muted" : undefined}>{shortCommit(code.head)}</span>
      {u ? (
        <Link to={`/runset/${rs.ref}?tab=code`} className="badge warn" style={{ marginLeft: 4 }}>
          +{u.files} {t("uncommitted")} <span className="diff-add">+{u.added}</span> <span className="diff-del">−{u.deleted}</span>
        </Link>
      ) : code.dirty ? (
        <span className="muted">*</span>
      ) : null}
    </span>
  );
}

function RunsetsTab({ ctx, showArchived, onShowArchived }: { ctx: NodeContext; showArchived: boolean; onShowArchived: (v: boolean) => void }) {
  const { t } = useI18n();
  const nav = useNavigate();
  const act = useAction();
  const invalidate = useInvalidateNode();
  const [selected, setSelected] = useState<string[]>([]);
  const shared = sharedTokens(ctx.runsets.map((rs) => paramTokens(rs.params, rs.code)));
  const extra = (ctx.params_differing ?? []).filter((k) => !coveredByTokens(k));
  const rank = (ref: string) => ctx.runsets.findIndex((r) => r.ref === ref);
  const toggle = (ref: string) => setSelected((old) => (old.includes(ref) ? old.filter((x) => x !== ref) : [...old, ref]));
  const archiveRefs = selected.filter((ref) => !ctx.runsets.find((rs) => rs.ref === ref)?.archived);
  const compare = () => {
    const [a, b] = [...selected].sort((x, y) => rank(x) - rank(y));
    nav(`/runset/${b}?tab=code&against=${encodeURIComponent(a)}`);
  };
  const archive = () => act.run(async () => {
    const chosen = ctx.runsets.filter((rs) => archiveRefs.includes(rs.ref));
    const active = chosen.find((rs) => rs.tasks.some((task) => ["queued", "launching", "running"].includes(task.state)));
    if (active) throw new Error(t("runset {ref} has active tasks; cancel them or wait", { ref: active.ref }));
    try {
      for (const ref of archiveRefs) await api.post("/runsets/archive", { ref, archived: true });
    } finally {
      await invalidate();
    }
    setSelected([]);
  });
  return (
    <div>
      <div className="row" style={{ marginBottom: 8 }}>
        <button className={`chip ${showArchived ? "active" : ""}`} onClick={() => { setSelected([]); onShowArchived(!showArchived); }}>
          {showArchived ? t("hide archived") : `${t("show archived")} (${ctx.archived_count ?? 0})`}
        </button>
        {selected.length === 2 && (
          <button className="primary" onClick={compare}>
            {t("compare")}
          </button>
        )}
        {archiveRefs.length > 0 && <button disabled={act.busy} onClick={archive}>{t("archive")} ({archiveRefs.length})</button>}
        {selected.length > 0 && <button onClick={() => setSelected([])}>{t("clear selection")}</button>}
        {!ctx.runsets.length && <span className="muted">{t("no runsets yet: use the submit tab.")}</span>}
      </div>
      <ErrorBox error={act.error} />
      {ctx.runsets.length > 0 && (
        <table>
          <thead>
            <tr>
              <th />
              <th>{t("runset")}</th>
              <th>{t("created")}</th>
              <th>{t("purpose")}</th>
              <th>{t("parameters")}</th>
              <th>{t("code")}</th>
              <th>{t("tasks")}</th>
              <th>{t("key metrics")}</th>
            </tr>
          </thead>
          <tbody>
            {ctx.runsets.map((rs) => (
              <tr key={rs.ref} className={selected.includes(rs.ref) ? "selected" : ""}>
                <td>
                  <input type="checkbox" checked={selected.includes(rs.ref)} onChange={() => toggle(rs.ref)} aria-label={`select ${rs.ref}`} />
                </td>
                <td className="mono">
                  <Link to={`/runset/${rs.ref}`} style={{ whiteSpace: "nowrap" }}>{rs.ref.slice(ctx.node.path.length + 1)}</Link> {rs.sealed && <Badge kind="sealed">{t("sealed")}</Badge>}
                  {rs.archived && <Badge kind="warn">{t("archived")}</Badge>}
                  {rs.derived_from && (
                    <div className="muted small" title={rs.derived_from}>
                      {t("from")}{" "}
                      {rs.derived_from.startsWith(`${ctx.node.path}/`) ? rs.derived_from.slice(ctx.node.path.length + 1) : rs.derived_from}
                    </div>
                  )}
                </td>
                <td className="muted">{fmtTime(rs.created)}</td>
                <td style={{ minWidth: 280 }}>{rs.purpose}</td>
                <td>
                  <ParamsCell rs={rs} shared={shared} extra={extra} single={ctx.runsets.length < 2} />
                </td>
                <td>
                  <CodeCell rs={rs} />
                </td>
                <td>
                  <TasksCell tasks={rs.tasks} />
                </td>
                <td className="small">
                  {keyMetrics(rs).map(([k, v]) => (
                    <div key={k}>
                      {metricLabel(k, t)} <b className="mono">{fmtValue(v)}</b>
                    </div>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {ctx.runset_count > ctx.runsets.length && (
        <p className="muted">
          {t("showing the latest {shown} of {total}", { shown: ctx.runsets.length, total: ctx.runset_count })}
        </p>
      )}
    </div>
  );
}

interface SubmitForm {
  kinds: string[];
  bin: string;
  points: string;
  postSteps: string;
  overrides: string;
  purpose: string;
  priority: string;
  quick: QuickPresets;
}

const EMPTY_FORM: SubmitForm = { kinds: ["build", "run", "post"], bin: "", points: "", postSteps: "", overrides: "", purpose: "", priority: "", quick: NO_PRESETS };
const COVERAGES = ["full", "0.8", "0.5", "0.3"];

/** A node remembers its whole form; a node never submitted from this browser starts from the
 * stages, presets and priority last used on any node. */
function initialForm(): SubmitForm {
  const last = loadStored<Partial<SubmitForm>>("submit:last", {});
  return { ...EMPTY_FORM, kinds: last.kinds ?? EMPTY_FORM.kinds, priority: last.priority ?? "", quick: { ...NO_PRESETS, ...(last.quick ?? {}) } };
}

function SubmitTab({ ctx }: { ctx: NodeContext }) {
  const { t } = useI18n();
  const nav = useNavigate();
  const qc = useQueryClient();
  const act = useAction();
  // folded sections stay unmounted until opened, so their editors and fetches only run on demand
  const hash = useLocation().hash;
  const [showRecipes, setShowRecipes] = useState(hash === "#recipes");
  const [showScripts, setShowScripts] = useState(hash === "#scripts");
  const [stored, setStored] = usePersistentState<SubmitForm>(`submit:${ctx.node.path}`, initialForm());
  const form: SubmitForm = { ...EMPTY_FORM, ...stored, quick: { ...NO_PRESETS, ...stored.quick } };
  const { kinds, bin, points, postSteps, overrides, purpose, priority, quick } = form;
  const setForm = (patch: Partial<SubmitForm>) => {
    const next = { ...form, ...patch };
    setStored(next);
    saveStored("submit:last", { kinds: next.kinds, quick: next.quick, priority: next.priority });
  };
  const setQuick = (q: QuickPresets) => setForm({ quick: q });
  const config = useQuery({ queryKey: ["config"], queryFn: () => api.get<ConfigView>("/config"), staleTime: 60000 });
  const workloadMap = (config.data?.effective.workloads ?? {}) as Record<string, { description?: string; label?: string; checkpoint?: string }>;
  const workloads = Object.entries(workloadMap);
  let parsed: Record<string, unknown> = {};
  let parseError: string | null = null;
  try {
    // the typed overrides win over the selectors
    parsed = deepMerge(presetOverrides(quick, kinds), parseOverrides(overrides));
  } catch (e) {
    parseError = (e as Error).message;
  }
  const prio = priority.trim();
  if (prio && !/^-?\d+$/.test(prio)) parseError ??= t('priority must be an integer, got "{value}"', { value: prio });
  const build = ctx.recipes.build ?? {};
  const run = ctx.recipes.run ?? {};
  const post = ctx.recipes.post ?? {};
  const opts = (build.options ?? {}) as Record<string, unknown>;
  const current = (v: unknown) => t("inherit ({value})", { value: v === undefined || v === null ? t("unset") : String(v) });
  const pick = (key: keyof QuickPresets, choices: [string, string][], now: unknown) => (
    <select value={quick[key]} onChange={(e) => setQuick({ ...quick, [key]: e.target.value })}>
      <option value="">{current(now)}</option>
      {choices.map(([v, label]) => (
        <option key={v} value={v}>
          {label}
        </option>
      ))}
    </select>
  );
  const onOff: [string, string][] = [
    ["on", "on"],
    ["off", "off"],
  ];
  // exec runs pick a checkpoint set; only sets of the chosen workload's SPEC version run with it
  const mode = String(quick.mode || run.mode || build.mode || "");
  const showCheckpoint = mode === "exec" || !!quick.checkpoint;
  const checkpoints = useQuery({ queryKey: ["checkpoints"], queryFn: () => api.get<CheckpointSet[]>("/checkpoints"), staleTime: 300000, enabled: showCheckpoint });
  const wlName = (quick.workload || run.workload || "") as string;
  const wlSpec = /spec(\d\d)/.exec(workloadMap[wlName]?.label ?? wlName)?.[1] ?? null;
  const ckptGroups = checkpointGroups(checkpoints.data ?? [], wlSpec);
  const ckptInherit = run.checkpoint
    ? t("inherit ({value})", { value: String(run.checkpoint) })
    : t("inherit ({checkpoint} — the workload's)", { checkpoint: workloadMap[wlName || String(config.data?.effective.default_workload ?? "")]?.checkpoint ?? t("the workload's") });
  const customCoverage = quick.coverage !== "" && !COVERAGES.includes(quick.coverage);
  const inheritedSpecScore = !!((post.steps ?? {}) as Record<string, unknown>)["spec-score"];
  const list = (s: string) => parseCsv(s.replace(/\s+/g, ","))[0]?.filter(Boolean) ?? [];
  const submit = () =>
    act.run(async () => {
      const r = await api.post<{ runset: string }>("/tasks", {
        node: ctx.node.path,
        kinds: KINDS.filter((k) => kinds.includes(k)),
        purpose,
        overrides: parsed,
        bin: bin || undefined,
        points: points.trim() ? list(points) : undefined,
        post_steps: postSteps.trim() ? list(postSteps) : undefined,
        priority: prio ? Number(prio) : undefined,
      });
      await qc.invalidateQueries();
      nav(`/runset/${r.runset}`);
    });
  return (
    <div className="submit-form">
      <div className="form">
        <label>{t("stages (required)")}</label>
        <div className="row">
          {KINDS.map((k) => (
            <label key={k}>
              <input type="checkbox" checked={kinds.includes(k)} onChange={(e) => setForm({ kinds: e.target.checked ? [...kinds, k] : kinds.filter((x) => x !== k) })} /> {t(k)}
            </label>
          ))}
          {!kinds.includes("build") && kinds.includes("run") && <span className="muted small">{t("without build the run recipe's emu (or --bin) is used")}</span>}
          <span className="spacer" />
          <button className="link small" onClick={() => setForm({ ...EMPTY_FORM })}>
            {t("reset form")}
          </button>
        </div>
        <label>{t("build")}</label>
        <div className="row">
          <span className="muted small">{t("tool")}</span>
          {pick("tool", [["gsim", "gsim"], ["verilator", "verilator"], ["script", "script"]], build.tool ?? build.type)}
          <span className="muted small">{t("mode")}</span>
          {pick("mode", config.data?.effective.trace_enabled ? [["trace", "trace"], ["exec", "exec"]] : [["exec", "exec"]], build.mode)}
          <span className="muted small">chiseldb</span>
          {pick("chiseldb", onOff, opts.chiseldb)}
          <span className="muted small">chiselmap</span>
          {pick("chiselmap", onOff, opts.chiselmap)}
        </div>
        <label>{t("run")}</label>
        <div className="row">
          <span className="muted small">{t("workload")}</span>
          {pick(
            "workload",
            workloads.map(([name, w]) => [name, w.description ? `${name} — ${w.description}` : name]),
            run.workload,
          )}
          {showCheckpoint && (
            <>
              <span className="muted small">{t("checkpoint")}</span>
              <select value={quick.checkpoint} onChange={(e) => setQuick({ ...quick, checkpoint: e.target.value })}>
                <option value="">{ckptInherit}</option>
                {quick.checkpoint && !ckptGroups.some(([, sets]) => sets.some((c) => c.name === quick.checkpoint)) && <option value={quick.checkpoint}>{quick.checkpoint}</option>}
                {ckptGroups.map(([spec, sets]) => (
                  <optgroup key={spec} label={`spec${spec}`}>
                    {sets.map((c) => (
                      <option key={c.name} value={c.name}>
                        {c.name} ({c.n_points} {t("points")}{c.restorer_needed ? ", .gz" : ""}{c.has_insts ? "" : `, ${t("no insts")}`})
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
              {checkpoints.isError && <span className="error small">{t("checkpoints:")} {(checkpoints.error as Error).message}</span>}
              <Link className="small" to="/docs/checkpoints">{t("sets")}</Link>
            </>
          )}
          <span className="muted small">{t("coverage")}</span>
          <select
            value={customCoverage ? "custom" : quick.coverage}
            onChange={(e) => setQuick({ ...quick, coverage: e.target.value === "custom" ? "0.9" : e.target.value })}
          >
            <option value="">{current(run.coverage)}</option>
            {COVERAGES.map((c) => (
              <option key={c} value={c}>
                {c === "full" ? t("full (all points)") : c}
              </option>
            ))}
            <option value="custom">{t("custom…")}</option>
          </select>
          {customCoverage && (
            <input type="text" className="mono narrow" aria-label={t("coverage")} value={quick.coverage} onChange={(e) => setQuick({ ...quick, coverage: e.target.value })} />
          )}
          <span className="muted small">W</span>
          <input type="text" className="mono narrow" value={quick.W} onChange={(e) => setQuick({ ...quick, W: e.target.value })} placeholder={String(run.W ?? "")} />
          <span className="muted small">I</span>
          <input type="text" className="mono narrow" value={quick.I} onChange={(e) => setQuick({ ...quick, I: e.target.value })} placeholder={String(run.I ?? "")} />
          <Link className="small" to="/docs/run">{t("keys")}</Link>
        </div>
        <label>{t("post")}</label>
        <div className="row">
          <label>
            <input type="checkbox" checked={inheritedSpecScore || quick.specScore === "on"} disabled={inheritedSpecScore} onChange={(e) => setQuick({ ...quick, specScore: e.target.checked ? "on" : "" })} /> spec-score
          </label>
          {inheritedSpecScore && <span className="muted small">{t("already in the post recipe")}</span>}
          <Link className="small" to="/docs/plugins">
            {t("plugins")}
          </Link>
        </div>
      </div>
      <fieldset className="optional-params">
        <legend>{t("Optional parameters")}</legend>
        <div className="form">
          <label>{t("priority")}</label>
          <div className="row">
            <input type="number" className="mono narrow" value={priority} placeholder="0" onChange={(e) => setForm({ priority: e.target.value })} />
          </div>
          <label>{t("purpose")}</label>
          <input type="text" value={purpose} onChange={(e) => setForm({ purpose: e.target.value })} placeholder={t("what this runset is meant to show")} />
          <label>{t("binary")}</label>
          <input type="text" className="mono" value={bin} onChange={(e) => setForm({ bin: e.target.value })} placeholder={t("bincache key, runset ref, or /abs/path/emu")} />
          <label>{t("points")}</label>
          <input type="text" className="mono" value={points} onChange={(e) => setForm({ points: e.target.value })} placeholder={t("comma or space separated")} />
          <label>{t("post steps")}</label>
          <input type="text" className="mono" value={postSteps} onChange={(e) => setForm({ postSteps: e.target.value })} placeholder={t("subset of the post recipe's steps")} />
          <label>{t("overrides")}</label>
          <div>
            <textarea className="mono" rows={4} value={overrides} onChange={(e) => setForm({ overrides: e.target.value })} placeholder={"run.W=20000000\nrun.I=20000000"} />
            {parseError ? <div className="error">{parseError}</div> : overrides.trim() && <pre className="mono small">{JSON.stringify(parsed)}</pre>}
          </div>
        </div>
      </fieldset>
      <div className="submit-actions">
        <button className="primary" disabled={act.busy || !!parseError || !kinds.length} onClick={submit}>
          {t("Submit")}
        </button>
        <span className="muted small">{t("same as:")} featx submit {ctx.node.path} {KINDS.filter((k) => kinds.includes(k)).join(" ")}
          {bin && ` --bin ${bin}`}
          {points.trim() && list(points).map((p) => ` --point ${p}`).join("")}
          {postSteps.trim() && list(postSteps).map((p) => ` --step ${p}`).join("")}
          {prio && prio !== "0" && ` --priority ${prio}`}
          {!parseError && setFlags(parsed).map((f) => ` ${f}`).join("")}
        </span>
        <ErrorBox error={act.error} />
      </div>
      <details className="tab-section" id="recipes" open={showRecipes} onToggle={(e) => setShowRecipes(e.currentTarget.open)}>
        <summary>{t("recipes")}</summary>
        {showRecipes && <RecipesTab ctx={ctx} />}
      </details>
      <details className="tab-section" id="scripts" open={showScripts} onToggle={(e) => setShowScripts(e.currentTarget.open)}>
        <summary>{t("scripts")}</summary>
        {showScripts && <ScriptsTab key={`scripts:${ctx.node.id}`} ctx={ctx} />}
      </details>
    </div>
  );
}

function RecipesTab({ ctx }: { ctx: NodeContext }) {
  const { t } = useI18n();
  const [kind, setKind] = useState<(typeof KINDS)[number]>("run");
  const own = ctx.own_recipes[kind] ?? {};
  const ownText = Object.keys(own).length ? YAML.stringify(own) : "";
  const [text, setText] = useState(ownText);
  const [explain, setExplain] = useState(false);
  const act = useAction();
  const invalidate = useInvalidateNode();
  useEffect(() => setText(ownText), [kind, ownText]);
  const ex = useQuery({
    queryKey: ["explain", ctx.node.path, kind],
    queryFn: () => api.get<string>("/recipes", { ref: ctx.node.path, kind, explain: true }),
    enabled: explain,
  });
  const save = () =>
    act.run(async () => {
      await api.post("/recipes", { ref: ctx.node.path, kind, data: text });
      await invalidate();
    });
  return (
    <div>
      <div className="row">
        <Tabs tabs={KINDS} value={kind} onChange={setKind} />
        <label>
          <input type="checkbox" checked={explain} onChange={(e) => setExplain(e.target.checked)} /> {t("explain (which layer set each key)")}
        </label>
      </div>
      <div className="grid2">
        <div>
          <h3>{t("This node's layer")}</h3>
          <YamlEditor value={text} onChange={setText} height="360px" />
          <div className="row" style={{ marginTop: 6 }}>
            <button className="primary" disabled={act.busy || text === ownText} onClick={save}>
              {t("Save {kind} recipe", { kind: t(kind) })}
            </button>
            <button disabled={text === ownText} onClick={() => setText(ownText)}>
              {t("Revert")}
            </button>
            <span className="muted small">{t("stored as YAML, comments are not kept")} · featx recipe set {ctx.node.path} {kind} -f FILE</span>
          </div>
          <ErrorBox error={act.error} />
        </div>
        <div>
          <h3>{t("Effective (root → ancestors → node)")}</h3>
          {explain ? (
            ex.isLoading ? <Loading /> : <pre className="logbox">{ex.data}</pre>
          ) : (
            <YamlEditor value={YAML.stringify(ctx.recipes[kind] ?? {})} readOnly height="360px" />
          )}
          <ErrorBox error={ex.error} />
        </div>
      </div>
    </div>
  );
}

function ScriptsTab({ ctx }: { ctx: NodeContext }) {
  const { t } = useI18n();
  const [sel, setSel] = useState<string | null>(ctx.scripts[0] ?? null);
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const act = useAction();
  const invalidate = useInvalidateNode();
  const q = useQuery({
    queryKey: ["script", ctx.node.path, sel],
    queryFn: () => api.get<string>("/scripts", { ref: ctx.node.path, name: sel }),
    enabled: !!sel,
  });
  useEffect(() => {
    if (sel && q.data !== undefined) {
      setName(sel);
      setText(q.data);
    }
  }, [sel, q.data]);
  const save = () =>
    act.run(async () => {
      await api.post("/scripts", { ref: ctx.node.path, name, content: text });
      setSel(name);
      await invalidate();
    });
  return (
    <div>
      <p className="muted small">
        {t("Saved in {path}. Build, run and post recipes call them as {script} (e.g. a post step {example}). Child nodes inherit them; each submit copies them into the runset.", { path: ctx.scripts_dir, script: "${scripts}/NAME", example: "cmd: bash ${scripts}/stats.sh ${run} ${out}" })}
      </p>
      <div className="chips">
        {ctx.scripts.map((s) => (
          <button key={s} className={`chip ${s === sel ? "active" : ""}`} onClick={() => setSel(s)}>
            {s}
          </button>
        ))}
        <button
          className="chip"
          onClick={() => {
            setSel(null);
            setName("");
            setText("#!/bin/bash\nset -euo pipefail\n");
          }}
        >
          {t("+ new")}
        </button>
      </div>
      <div className="row" style={{ margin: "8px 0" }}>
        <input type="text" className="mono" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("script name")} />
        <button className="primary" disabled={!name || act.busy} onClick={save}>
          {t("Save (executable)")}
        </button>
      </div>
      <textarea className="mono" rows={18} style={{ width: "100%" }} value={text} onChange={(e) => setText(e.target.value)} />
      <ErrorBox error={act.error ?? q.error} />
    </div>
  );
}

interface WtStatus {
  worktree: string;
  branch: string;
  base: string;
  exists: boolean;
  head?: string;
  dirty?: number;
  error?: string;
  submodules?: Record<string, { branch: string; path: string; head: string; dirty: number; base: string; ahead?: number; error?: string }>;
}

function CreateWorktreeDialog({ path, onClose }: { path: string; onClose: () => void }) {
  const { t } = useI18n();
  const act = useAction();
  const invalidate = useInvalidateNode();
  const [repo, setRepo] = useState("");
  const [base, setBase] = useState("");
  return (
    <Dialog open onClose={onClose} title={t("Create a worktree for {path}", { path })}>
      <div className="form">
        <label>{t("repo")}</label>
        <input type="text" className="mono" autoFocus value={repo} onChange={(e) => setRepo(e.target.value)} placeholder={t("default: git.repo from featx.yaml")} />
        <label>{t("base ref")}</label>
        <input type="text" className="mono" value={base} onChange={(e) => setBase(e.target.value)} placeholder={t("default: git.base from featx.yaml; resolved at creation")} />
      </div>
      <p className="muted small">{t("Creates the worktree and branch featx/{path} (and the configured submodule worktrees). This takes a while.", { path })}</p>
      <ErrorBox error={act.error} />
      <div className="dialog-actions">
        <button onClick={onClose}>{t("Cancel")}</button>
        <button
          className="primary"
          disabled={act.busy}
          onClick={() =>
            act.run(async () => {
              await api.post("/worktrees", { ref: path, repo: repo || undefined, base: base || undefined });
              await invalidate();
              onClose();
            })
          }
        >
          {act.busy ? t("Creating…") : t("Create")}
        </button>
      </div>
    </Dialog>
  );
}

function WorktreeTab({ ctx }: { ctx: NodeContext }) {
  const { t } = useI18n();
  const [creating, setCreating] = useState(false);
  const q = useQuery({
    queryKey: ["wt", ctx.node.path],
    queryFn: () => api.get<WtStatus>("/worktrees/status", { ref: ctx.node.path }),
    enabled: !!ctx.git,
  });
  if (!ctx.git) {
    // ${worktree} only resolves from the node's own git info (recipes.base_context), so a
    // build of this node is refused until it has one; runs with --bin do not need it.
    return (
      <div>
        <p>
          {t("No worktree. Builds of this node need one ({worktree}); runs with an existing binary (--bin) don't.", { worktree: "${worktree}" })}
        </p>
        <div className="row">
          <button onClick={() => setCreating(true)}>{t("Create worktree…")}</button>
          <span className="muted small">featx worktree create {ctx.node.path}</span>
        </div>
        {creating && <CreateWorktreeDialog path={ctx.node.path} onClose={() => setCreating(false)} />}
      </div>
    );
  }
  const g = ctx.git;
  const base = q.data?.base ?? g.base;
  // a legacy node (or a daemon that has not reloaded yet) may still carry a ref here
  const baseIsSha = /^[0-9a-f]{40}$/i.test(base);
  return (
    <div>
      <div className="kv">
        <span>worktree</span>
        <span className="mono">{g.worktree}</span>
        <span>{t("repo")}</span>
        <span className="mono">{g.repo}</span>
        <span>{t(baseIsSha ? "base commit" : "base ref")}</span>
        <span className="mono" title={base}>{shortCommit(base)}</span>
        <span>{t("branch / checkout")}</span>
        <span className="mono">
          {g.branch === "HEAD" ? t("detached at pinned commit {head}", { head: shortCommit(q.data?.head) }) : g.branch}
        </span>
        {q.data && (
          <>
            <span>HEAD</span>
            <span className="mono" title={q.data.head}>{shortCommit(q.data.head)}</span>
            <span>{t("uncommitted state")}</span>
            <span>
              {!q.data.exists ? (
                <Badge kind="failed">{t("missing on disk")}</Badge>
              ) : q.data.dirty ? (
                <Badge kind="warn">{t("{count} uncommitted change(s)", { count: q.data.dirty })}</Badge>
              ) : (
                <Badge kind="succeeded">{t("no uncommitted changes")}</Badge>
              )}
            </span>
          </>
        )}
      </div>
      {q.data?.error && <p className="muted small">{q.data.error}{t("; repair it with featx worktree set-base.")}</p>}
      {q.data?.submodules && Object.keys(q.data.submodules).length > 0 && (
        <>
          <h3>{t("submodule worktrees")}</h3>
          <table className="small">
            <thead><tr><th>{t("repo")}</th><th>{t("branch")}</th><th>{t("base commit")}</th><th>HEAD</th><th>{t("uncommitted")}</th></tr></thead>
            <tbody>
              {Object.entries(q.data.submodules).map(([name, s]) => (
                <Fragment key={name}>
                  <tr>
                    <td className="mono" title={s.path}>{name}</td>
                    <td className="mono">{s.branch}</td>
                    <td className="mono" title={s.base}>{shortCommit(s.base)}</td>
                    <td className="mono" title={s.head}>{shortCommit(s.head)}</td>
                    <td>{s.dirty}</td>
                  </tr>
                  {s.error && <tr><td colSpan={5} className="notice small">{s.error}</td></tr>}
                </Fragment>
              ))}
            </tbody>
          </table>
        </>
      )}
      <ErrorBox error={q.error} />
      {q.data?.exists !== false && <ChangesSection path={ctx.node.path} />}
    </div>
  );
}

function evidenceLink(ev: string) {
  return ev.includes("/") && !ev.includes(" ") ? <Link to={`/runset/${ev}`}>{ev}</Link> : ev;
}

function EvidenceList({ evidence, archived, busy, save, add }: { evidence: string[]; archived: Set<string>; busy: boolean; save: (list: string[]) => Promise<unknown>; add: (ev: string) => Promise<unknown> }) {
  const { t } = useI18n();
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [fresh, setFresh] = useState("");
  const commitEdit = () => {
    const text = draft.trim();
    if (editing === null) return;
    const next = text ? evidence.map((e, i) => (i === editing ? text : e)) : evidence.filter((_, i) => i !== editing);
    save(next).then((ok) => ok && setEditing(null));
  };
  const addFresh = () => {
    if (fresh.trim()) add(fresh.trim()).then((ok) => ok && setFresh(""));
  };
  return (
    <div>
      {evidence.map((ev, i) =>
        editing === i ? (
          <div key={`edit:${i}`} className="row evidence-row">
            <textarea
              className="mono"
              rows={Math.min(6, Math.max(1, draft.split("\n").length))}
              style={{ flex: 1 }}
              autoFocus
              value={draft}
              aria-label={t("edit evidence")}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) commitEdit();
                if (e.key === "Escape") setEditing(null);
              }}
            />
            <button disabled={busy || draft.trim() === ev} onClick={commitEdit}>
              {t("Save")}
            </button>
            <button onClick={() => setEditing(null)}>{t("Cancel")}</button>
          </div>
        ) : (
          <div key={`ev:${i}`} className="row evidence-row">
            <span className="mono small" style={{ flex: 1, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
              {evidenceLink(ev)} {archived.has(ev) && <Badge kind="warn">{t("archived")}</Badge>}
            </span>
            <button
              className="link small"
              title={t("edit this entry")}
              onClick={() => {
                setEditing(i);
                setDraft(ev);
              }}
            >
              {t("edit")}
            </button>
            <button className="link small danger-link" title={t("remove this entry")} aria-label={t("remove evidence {index}", { index: i + 1 })} disabled={busy} onClick={() => save(evidence.filter((_, j) => j !== i))}>
              ×
            </button>
          </div>
        ),
      )}
      <div className="row" style={{ alignItems: "flex-start" }}>
        <textarea
          className="mono"
          rows={2}
          style={{ flex: 1 }}
          value={fresh}
          placeholder={t("runset ref or note (Ctrl+Enter adds)")}
          onChange={(e) => setFresh(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) addFresh();
          }}
        />
        <button disabled={!fresh.trim() || busy} onClick={addFresh}>
          {t("Add evidence")}
        </button>
      </div>
    </div>
  );
}

function useChanges(path: string) {
  return useQuery({
    queryKey: ["wt-changes", path],
    queryFn: () => api.get<Changes>("/worktrees/changes", { ref: path }),
    refetchInterval: 30000,
    staleTime: 10000,
  });
}

function ChangesBadge({ path }: { path: string }) {
  const { t: translate, language } = useI18n();
  const q = useChanges(path);
  const t = q.data?.total;
  if (!t || (!t.files && !t.ahead)) return null;
  return (
    <Link to={`/node/${path}?tab=worktree`} title={translate("code that differs from the base (see the worktree tab)")}>
      <Badge kind="warn">
        {translate("code changed: {files} file{suffix}, +{added} −{deleted}", { files: t.files, suffix: language === "zh" || t.files === 1 ? "" : "s", added: t.added, deleted: t.deleted })}
        {t.ahead ? translate(", {count} commit{suffix} since base", { count: t.ahead, suffix: language === "zh" || t.ahead === 1 ? "" : "s" }) : ""}
      </Badge>
    </Link>
  );
}

function ChangesSection({ path }: { path: string }) {
  const { t } = useI18n();
  const q = useChanges(path);
  if (q.isLoading) return <Loading what="changes" />;
  if (q.error) return <ErrorBox error={q.error} />;
  return (
    <div>
      <h2>{t("Committed + working-tree changes vs base")}</h2>
      <p className="muted small">{t("Submodule gitlinks are excluded from the superproject and shown in each submodule's own section.")}</p>
      <ChangesView
        changes={q.data}
        scope={`wt:${path}`}
        fetchDiff={(sub, file) => api.get("/worktrees/diff", { ref: path, sub, path: file })}
      />
    </div>
  );
}

function NotesTab({ ctx }: { ctx: NodeContext }) {
  const { t } = useI18n();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(ctx.notes ?? "");
  const act = useAction();
  const invalidate = useInvalidateNode();
  useEffect(() => {
    if (!editing) setText(ctx.notes ?? "");
  }, [ctx.notes, editing]);
  const save = () =>
    act.run(async () => {
      await api.post("/nodes/update", { ref: ctx.node.path, notes: text });
      await invalidate();
      setEditing(false);
    });
  if (!editing) {
    return (
      <div>
        <div className="row" style={{ marginBottom: 8 }}>
          <button onClick={() => setEditing(true)}>{t("Edit")}</button>
          <span className="muted small">{t("Markdown, stored in the node's node.yaml")} · featx node update {ctx.node.path} --notes …</span>
        </div>
        {ctx.notes ? <Markdown text={ctx.notes} /> : <div className="muted">{t("No notes yet — Edit to add.")}</div>}
      </div>
    );
  }
  return (
    <div>
      <div className="grid2">
        <div>
          <textarea className="mono" rows={22} value={text} aria-label={t("notes")} autoFocus onChange={(e) => setText(e.target.value)} placeholder={t("# Notes\n\nMarkdown")} />
        </div>
        <div className="card" style={{ overflow: "auto", maxHeight: "60vh" }}>
          {text.trim() ? <Markdown text={text} /> : <span className="muted">{t("preview")}</span>}
        </div>
      </div>
      <div className="row" style={{ marginTop: 6 }}>
        <button className="primary" disabled={act.busy || text === (ctx.notes ?? "")} onClick={save}>
          {t("Save notes")}
        </button>
        <button
          onClick={() => {
            setText(ctx.notes ?? "");
            setEditing(false);
          }}
        >
          {t("Cancel")}
        </button>
      </div>
      <ErrorBox error={act.error} />
    </div>
  );
}

function Header({ ctx }: { ctx: NodeContext }) {
  const { t } = useI18n();
  const n = ctx.node;
  const nav = useNavigate();
  const hash = useLocation().hash;
  const act = useAction();
  const invalidate = useInvalidateNode();
  const [hyp, setHyp] = useState(n.hypothesis);
  const [note, setNote] = useState(n.verdict_note);
  const [comment, setComment] = useState(n.comment ?? "");
  const [dialog, setDialog] = useState<"add" | "mv" | "rm" | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(true);
  useEffect(() => {
    setHyp(n.hypothesis);
    setNote(n.verdict_note);
    setComment(n.comment ?? "");
  }, [n.hypothesis, n.verdict_note, n.comment]);
  useEffect(() => {
    if (hash === "#verdict-evidence") setDetailsOpen(true);
  }, [hash]);
  const update = (fields: Record<string, unknown>) =>
    act.run(async () => {
      await api.post("/nodes/update", { ref: n.path, ...fields });
      await invalidate();
      return true;
    });
  return (
    <div>
      <div className="crumbs">
        {ctx.ancestors.map((a) => (
          <span key={a.path}>
            <Link to={`/node/${a.path}`}>{a.path.split("/").pop()}</Link> /{" "}
          </span>
        ))}
      </div>
      <div className="row">
        <CopyTitle text={n.path}>
          <VerdictMark verdict={n.verdict} /> {n.path.split("/").pop()}
        </CopyTitle>
        <Badge>{t(n.kind)}</Badge>
        {n.tags.map((t) => (
          <Badge key={t} kind="tag">
            #{t}
          </Badge>
        ))}
        {ctx.git && <ChangesBadge path={n.path} />}
        <span className="spacer" />
        <button onClick={() => setDialog("add")}>{t("Add child…")}</button>
        <button onClick={() => setDialog("mv")}>{t("Move…")}</button>
        <button className="danger" onClick={() => setDialog("rm")}>
          {t("Remove…")}
        </button>
      </div>
      <details className="node-details" id="verdict-evidence" open={detailsOpen} onToggle={(e) => setDetailsOpen(e.currentTarget.open)}>
        <summary>{t("Node details")}</summary>
        <div className="card form">
          <label>{t("hypothesis")}</label>
          <div className="row">
            <textarea rows={2} style={{ flex: 1 }} value={hyp} onChange={(e) => setHyp(e.target.value)} />
            <button disabled={hyp === n.hypothesis || act.busy} onClick={() => update({ hypothesis: hyp })}>
              {t("Save")}
            </button>
          </div>
          <label>{t("verdict")}</label>
          <div className="row">
            <select value={n.verdict} onChange={(e) => update({ verdict: e.target.value })}>
              {VERDICTS.map((v) => (
                <option key={v} value={v}>{t(v)}</option>
              ))}
            </select>
            <input type="text" style={{ flex: 1 }} value={note} placeholder={t("verdict note")} onChange={(e) => setNote(e.target.value)} />
            <button disabled={note === n.verdict_note || act.busy} onClick={() => update({ verdict_note: note })}>
              {t("Save note")}
            </button>
            <label>{t("status")}</label>
            <select value={n.status} onChange={(e) => update({ status: e.target.value })}>
              {STATUSES.map((v) => (
                <option key={v} value={v}>{t(v)}</option>
              ))}
            </select>
          </div>
          <label>{t("evidence")}</label>
          <EvidenceList
            evidence={n.evidence}
            archived={new Set(ctx.archived_runsets ?? [])}
            busy={act.busy}
            save={(list) => update({ evidence: list })}
            add={(ev) => update({ add_evidence: [ev] })}
          />
          <label>{t("comment")}</label>
          <div className="row" style={{ alignItems: "flex-start" }}>
            <textarea rows={3} style={{ flex: 1, fontFamily: "inherit", fontSize: 13 }} value={comment} placeholder={t("your thoughts on this node: ideas, doubts, what to try next")} onChange={(e) => setComment(e.target.value)} />
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <button disabled={comment === (n.comment ?? "") || act.busy} onClick={() => update({ comment })}>
                {t("Save")}
              </button>
              {comment !== (n.comment ?? "") && <span className="muted small">{t("unsaved")}</span>}
            </div>
          </div>
        </div>
      </details>
      <ErrorBox error={act.error} />
      {dialog === "add" && <AddNodeDialog parent={n.path} onClose={() => setDialog(null)} onDone={(p) => nav(`/node/${p}`)} />}
      {dialog === "mv" && <MoveDialog src={n.path} onClose={() => setDialog(null)} onDone={(dst) => nav(`/node/${dst}`, { replace: true })} />}
      {dialog === "rm" && (
        <RemoveDialog
          path={n.path}
          onClose={() => setDialog(null)}
          onDone={(r) => {
            const parent = n.path.includes("/") ? `/node/${n.path.slice(0, n.path.lastIndexOf("/"))}` : "/leaderboard";
            nav(parent, { replace: true, state: { notice: `Removed ${n.path}: ${JSON.stringify(r)}` } });
          }}
        />
      )}
    </div>
  );
}

export function NodePage() {
  const { t } = useI18n();
  const path = useParams()["*"] ?? "";
  const notice = (useLocation().state as { notice?: string } | null)?.notice;
  const [sp, setSp] = useSearchParams();
  const [showArchived, setShowArchived] = usePersistentState("node:showArchived", false);
  const requestedTab = sp.get("tab") ?? "";
  const tab = (TABS as readonly string[]).includes(requestedTab) ? (requestedTab as Tab) : NODE_TAB_ALIASES[requestedTab] ?? "runsets";
  const q = useQuery({
    queryKey: ["node", path, showArchived],
    queryFn: () => api.get<NodeContext>("/nodes/context", { ref: path, max_runsets: 50, include_archived: showArchived }),
    refetchInterval: 10000,
  });
  if (q.isLoading) return <Loading what={path} />;
  if (q.error) return <ErrorBox error={q.error} />;
  const ctx = q.data!;
  return (
    <div>
      {notice && <div className="okbox mono small">{notice}</div>}
      {/* sibling keys must differ: a shared key made React leave a stale Header behind when ScriptsTab unmounted */}
      <Header key={`header:${ctx.node.id}`} ctx={ctx} />
      <Tabs tabs={TABS} value={tab} onChange={(t) => setSp({ tab: t }, { replace: true })} />
      {tab === "runsets" && <RunsetsTab ctx={ctx} showArchived={showArchived} onShowArchived={setShowArchived} />}
      {tab === "submit" && <SubmitTab ctx={ctx} />}
      {tab === "worktree" && (
        <section className="tab-section" id="worktree">
          <WorktreeTab ctx={ctx} />
        </section>
      )}
      {tab === "notes" && (
        <section className="tab-section" id="notes">
          <NotesTab key={`notes:${ctx.node.id}`} ctx={ctx} />
        </section>
      )}
      {ctx.children.length > 0 && (
        <>
          <h2>{t("Children")}</h2>
          <ul>
            {ctx.children.map((c) => (
              <li key={c.path}>
                <VerdictMark verdict={c.verdict} />{" "}
                <Link to={`/node/${c.path}`} title={c.path}>
                  {c.path.slice(ctx.node.path.length + 1)}
                </Link>{" "}
                <span className="muted">{c.hypothesis}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
