import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import YAML from "yaml";
import { api } from "../api";
import { AddNodeDialog, MoveDialog, RemoveDialog } from "../components/NodeDialogs";
import { Markdown } from "../components/FileViewer";
import { Badge, Dialog, ErrorBox, Loading, StateBadge, Tabs, VerdictMark, useAction } from "../components/ui";
import { YamlEditor } from "../components/YamlEditor";
import type { CheckpointSet, ConfigView, NodeContext, RunsetSummary } from "../types";
import { NO_PRESETS, checkpointGroups, deepMerge, fmtTime, fmtValue, loadStored, metricLabel, parseCsv, parseOverrides, presetOverrides, saveStored, setFlags, usePersistentState } from "../util";
import type { QuickPresets } from "../util";
import { useI18n } from "../i18n";

const VERDICTS = ["open", "supported", "refuted", "inconclusive"];
const STATUSES = ["active", "parked", "done", "abandoned"];
const KINDS = ["build", "run", "post"] as const;
const TABS = ["runsets", "submit", "recipes", "scripts", "worktree", "notes"] as const;
type Tab = (typeof TABS)[number];

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

function RunsetsTab({ ctx }: { ctx: NodeContext }) {
  if (!ctx.runsets.length) return <div className="muted">no runsets yet: use the submit tab.</div>;
  return (
    <div>
      <table>
        <thead>
          <tr>
            <th>runset</th>
            <th>created</th>
            <th>purpose</th>
            <th>tasks</th>
            <th>key metrics</th>
          </tr>
        </thead>
        <tbody>
          {ctx.runsets.map((rs) => (
            <tr key={rs.ref}>
              <td className="mono">
                <Link to={`/runset/${rs.ref}`}>{rs.ref.slice(ctx.node.path.length + 1)}</Link> {rs.sealed && <Badge kind="sealed">sealed</Badge>}
                {rs.derived_from && <div className="muted small">from {rs.derived_from}</div>}
              </td>
              <td className="muted">{fmtTime(rs.created)}</td>
              <td>{rs.purpose}</td>
              <td>
                {rs.tasks.map((t) => (
                  <span key={t.id} style={{ marginRight: 4 }}>
                    {t.kind}: <StateBadge state={t.state} />
                  </span>
                ))}
              </td>
              <td className="small">
                {keyMetrics(rs).map(([k, v]) => (
                  <div key={k}>
                    {metricLabel(k)} <b className="mono">{fmtValue(v)}</b>
                  </div>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {ctx.runset_count > ctx.runsets.length && (
        <p className="muted">
          showing the latest {ctx.runsets.length} of {ctx.runset_count}
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
  if (prio && !/^-?\d+$/.test(prio)) parseError ??= `priority must be an integer, got "${prio}"`;
  const build = ctx.recipes.build ?? {};
  const run = ctx.recipes.run ?? {};
  const post = ctx.recipes.post ?? {};
  const opts = (build.options ?? {}) as Record<string, unknown>;
  const current = (v: unknown) => `inherit (${v === undefined || v === null ? "unset" : String(v)})`;
  const pick = (key: keyof QuickPresets, choices: [string, string][], now: unknown, title?: string) => (
    <select value={quick[key]} title={title} onChange={(e) => setQuick({ ...quick, [key]: e.target.value })}>
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
    ? `inherit (${String(run.checkpoint)})`
    : `inherit (${workloadMap[wlName || String(config.data?.effective.default_workload ?? "")]?.checkpoint ?? "the workload's"} — the workload's)`;
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
    <div className="form">
      <label>{t("stages")}</label>
      <div className="row">
        {KINDS.map((k) => (
          <label key={k}>
            <input type="checkbox" checked={kinds.includes(k)} onChange={(e) => setForm({ kinds: e.target.checked ? [...kinds, k] : kinds.filter((x) => x !== k) })} /> {k}
          </label>
        ))}
        {!kinds.includes("build") && kinds.includes("run") && <span className="muted small">without build the run recipe's emu (or --bin) is used</span>}
        <span className="spacer" />
        <button className="link small" title={t("reset form")} onClick={() => setForm({ ...EMPTY_FORM })}>
          {t("reset form")}
        </button>
      </div>
      <label>{t("build")}</label>
      <div className="row">
        <span className="muted small">tool</span>
        {pick("tool", [["gsim", "gsim"], ["verilator", "verilator"], ["script", "script"]], build.tool ?? build.type)}
        <span className="muted small">mode</span>
        {pick("mode", [["trace", "trace"], ["exec", "exec"]], build.mode, "TRACERTL_MODE; the run follows the build")}
        <span className="muted small">chiseldb</span>
        {pick("chiseldb", onOff, opts.chiseldb)}
        <span className="muted small">chiselmap</span>
        {pick("chiselmap", onOff, opts.chiselmap, "WITH_CHISELMAP=1 (placeholder: no Makefile reads it yet)")}
      </div>
      <label>{t("run")}</label>
      <div className="row">
        <span className="muted small">{t("workload")}</span>
        {pick(
          "workload",
          workloads.map(([name, w]) => [name, w.description ? `${name} — ${w.description}` : name]),
          run.workload,
          "all its points unless points / benchmarks narrow them",
        )}
        {showCheckpoint && (
          <>
            <span className="muted small">{t("checkpoint")}</span>
            <select
              value={quick.checkpoint}
              title={`exec runs: the SimPoint checkpoint set (run.checkpoint)${wlSpec ? `; spec${wlSpec} sets only, as the workload is ${wlName}` : ""}`}
              onChange={(e) => setQuick({ ...quick, checkpoint: e.target.value })}
            >
              <option value="">{ckptInherit}</option>
              {quick.checkpoint && !ckptGroups.some(([, sets]) => sets.some((c) => c.name === quick.checkpoint)) && <option value={quick.checkpoint}>{quick.checkpoint}</option>}
              {ckptGroups.map(([spec, sets]) => (
                <optgroup key={spec} label={`spec${spec}`}>
                  {sets.map((c) => (
                    <option key={c.name} value={c.name}>
                      {c.name} ({c.n_points} points{c.restorer_needed ? ", .gz" : ""}{c.has_insts ? "" : ", no insts"})
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            {checkpoints.isError && <span className="error small">checkpoints: {(checkpoints.error as Error).message}</span>}
            <Link className="small" to="/docs/checkpoints">
              sets
            </Link>
          </>
        )}
        <span className="muted small">{t("coverage")}</span>
        <select
          value={customCoverage ? "custom" : quick.coverage}
          title="per benchmark, the heaviest points until their weights sum to c (0.3 = the -0.3c configs)"
          onChange={(e) => setQuick({ ...quick, coverage: e.target.value === "custom" ? "0.9" : e.target.value })}
        >
          <option value="">{current(run.coverage)}</option>
          {COVERAGES.map((c) => (
            <option key={c} value={c}>
              {c === "full" ? "full (all points)" : c}
            </option>
          ))}
          <option value="custom">custom…</option>
        </select>
        {customCoverage && (
          <input type="text" className="mono narrow" aria-label="coverage" value={quick.coverage} onChange={(e) => setQuick({ ...quick, coverage: e.target.value })} />
        )}
        <span className="muted small">W</span>
        <input type="text" className="mono narrow" value={quick.W} onChange={(e) => setQuick({ ...quick, W: e.target.value })} placeholder={String(run.W ?? "")} />
        <span className="muted small">I</span>
        <input type="text" className="mono narrow" value={quick.I} onChange={(e) => setQuick({ ...quick, I: e.target.value })} placeholder={String(run.I ?? "")} />
        <Link className="small" to="/docs/run">
          keys
        </Link>
      </div>
      <label>{t("post")}</label>
      <div className="row">
        <label title="SPEC score as perf_trigger --report computes it (spec-score plugin)">
          <input type="checkbox" checked={inheritedSpecScore || quick.specScore === "on"} disabled={inheritedSpecScore} onChange={(e) => setQuick({ ...quick, specScore: e.target.checked ? "on" : "" })} /> spec-score
        </label>
        {inheritedSpecScore && <span className="muted small">already in the post recipe</span>}
        <Link className="small" to="/docs/plugins">
          plugins
        </Link>
      </div>
      <label>{t("priority")}</label>
      <div className="row">
        <input type="number" className="mono narrow" value={priority} placeholder="0" onChange={(e) => setForm({ priority: e.target.value })} />
        <span className="muted small">higher launches first (FIFO within a priority); running jobs are never preempted</span>
      </div>
      <label>{t("purpose")}</label>
      <input type="text" value={purpose} onChange={(e) => setForm({ purpose: e.target.value })} placeholder={t("what this runset is meant to show")} />
      <label>binary</label>
      <input type="text" className="mono" value={bin} onChange={(e) => setForm({ bin: e.target.value })} placeholder={t("optional: bincache key, runset ref, or /abs/path/emu")} />
      <label>points</label>
      <input type="text" className="mono" value={points} onChange={(e) => setForm({ points: e.target.value })} placeholder={t("optional subset, comma or space separated")} />
      <label>{t("post steps")}</label>
      <input type="text" className="mono" value={postSteps} onChange={(e) => setForm({ postSteps: e.target.value })} placeholder={t("optional subset of the post recipe's steps")} />
      <label>{t("overrides")}</label>
      <div>
        <textarea className="mono" rows={4} value={overrides} onChange={(e) => setForm({ overrides: e.target.value })} placeholder={"run.W=20000000\nrun.I=20000000"} />
        {parseError ? <div className="error">{parseError}</div> : overrides.trim() && <pre className="mono small">{JSON.stringify(parsed)}</pre>}
      </div>
      <span />
      <div>
        <button className="primary" disabled={act.busy || !!parseError || !kinds.length} onClick={submit}>
          {t("Submit")}
        </button>
        <span className="muted small mono">
          {" "}
          same as: featx submit {ctx.node.path} {KINDS.filter((k) => kinds.includes(k)).join(" ")}
          {bin && ` --bin ${bin}`}
          {points.trim() && list(points).map((p) => ` --point ${p}`).join("")}
          {postSteps.trim() && list(postSteps).map((p) => ` --step ${p}`).join("")}
          {prio && prio !== "0" && ` --priority ${prio}`}
          {!parseError && setFlags(parsed).map((f) => ` ${f}`).join("")}
        </span>
        <ErrorBox error={act.error} />
      </div>
    </div>
  );
}

function RecipesTab({ ctx }: { ctx: NodeContext }) {
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
          <input type="checkbox" checked={explain} onChange={(e) => setExplain(e.target.checked)} /> explain (which layer set each key)
        </label>
      </div>
      <div className="grid2">
        <div>
          <h3>This node's layer</h3>
          <YamlEditor value={text} onChange={setText} height="360px" />
          <div className="row" style={{ marginTop: 6 }}>
            <button className="primary" disabled={act.busy || text === ownText} onClick={save}>
              Save {kind} recipe
            </button>
            <button disabled={text === ownText} onClick={() => setText(ownText)}>
              Revert
            </button>
            <span className="muted small">stored as YAML, comments are not kept · featx recipe set {ctx.node.path} {kind} -f FILE</span>
          </div>
          <ErrorBox error={act.error} />
        </div>
        <div>
          <h3>Effective (root → ancestors → node)</h3>
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
        Saved in <span className="mono">{ctx.scripts_dir}</span>. Build, run and post recipes call them as{" "}
        <span className="mono">{"${scripts}"}/NAME</span> (e.g. a post step <span className="mono">cmd: bash {"${scripts}"}/stats.sh {"${run} ${out}"}</span>).
        Child nodes inherit them; each submit copies them into the runset.
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
          + new
        </button>
      </div>
      <div className="row" style={{ margin: "8px 0" }}>
        <input type="text" className="mono" value={name} onChange={(e) => setName(e.target.value)} placeholder="script name" />
        <button className="primary" disabled={!name || act.busy} onClick={save}>
          Save (executable)
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
  exists: boolean;
  head?: string;
  dirty?: number;
  submodules?: Record<string, { branch: string; path: string; head: string; dirty: number }>;
}

function CreateWorktreeDialog({ path, onClose }: { path: string; onClose: () => void }) {
  const act = useAction();
  const invalidate = useInvalidateNode();
  const [repo, setRepo] = useState("");
  const [base, setBase] = useState("");
  return (
    <Dialog open onClose={onClose} title={`Create a worktree for ${path}`}>
      <div className="form">
        <label>repo</label>
        <input type="text" className="mono" autoFocus value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="default: git.repo from featx.yaml" />
        <label>base</label>
        <input type="text" className="mono" value={base} onChange={(e) => setBase(e.target.value)} placeholder="default: git.base from featx.yaml" />
      </div>
      <p className="muted small">Creates the worktree and branch featx/{path} (and the configured submodule worktrees). This takes a while.</p>
      <ErrorBox error={act.error} />
      <div className="dialog-actions">
        <button onClick={onClose}>Cancel</button>
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
          {act.busy ? "Creating…" : "Create"}
        </button>
      </div>
    </Dialog>
  );
}

function WorktreeTab({ ctx }: { ctx: NodeContext }) {
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
          No worktree. Builds of this node need one (<span className="mono">{"${worktree}"}</span>); runs with an
          existing binary (<span className="mono">--bin</span>) don't.
        </p>
        <div className="row">
          <button onClick={() => setCreating(true)}>Create worktree…</button>
          <span className="muted small">featx worktree create {ctx.node.path}</span>
        </div>
        {creating && <CreateWorktreeDialog path={ctx.node.path} onClose={() => setCreating(false)} />}
      </div>
    );
  }
  const g = ctx.git;
  return (
    <div>
      <div className="kv">
        <span>worktree</span>
        <span className="mono">{g.worktree}</span>
        <span>branch</span>
        <span className="mono">{g.branch}</span>
        <span>repo / base</span>
        <span className="mono">
          {g.repo} @ {g.base}
        </span>
        {q.data && (
          <>
            <span>head</span>
            <span className="mono">{q.data.head}</span>
            <span>state</span>
            <span>
              {!q.data.exists ? (
                <Badge kind="failed">missing on disk</Badge>
              ) : q.data.dirty ? (
                <Badge kind="warn">{q.data.dirty} uncommitted change(s)</Badge>
              ) : (
                <Badge kind="succeeded">clean</Badge>
              )}
            </span>
          </>
        )}
      </div>
      {q.data?.submodules && Object.keys(q.data.submodules).length > 0 && (
        <>
          <h3>submodule worktrees</h3>
          <pre className="mono">{YAML.stringify(q.data.submodules)}</pre>
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

function EvidenceList({ evidence, busy, save, add }: { evidence: string[]; busy: boolean; save: (list: string[]) => Promise<unknown>; add: (ev: string) => Promise<unknown> }) {
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
              aria-label="edit evidence"
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
              {evidenceLink(ev)}
            </span>
            <button
              className="link small"
              title={t("edit this entry")}
              onClick={() => {
                setEditing(i);
                setDraft(ev);
              }}
            >
              {t("edit this entry")}
            </button>
            <button className="link small danger-link" title={t("remove this entry")} aria-label={`remove evidence ${i + 1}`} disabled={busy} onClick={() => save(evidence.filter((_, j) => j !== i))}>
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

interface RepoChanges {
  path: string;
  base: string;
  exists: boolean;
  error?: string;
  merge_base?: string;
  ahead?: number;
  behind?: number;
  commits?: { sha: string; author: string; when: string; subject: string }[];
  files?: { status: string; path: string; old_path: string | null; added: number | null; deleted: number | null; uncommitted: boolean }[];
  untracked?: string[];
  truncated?: boolean;
  added?: number;
  deleted?: number;
}

interface Changes {
  repos: Record<string, RepoChanges>;
  total?: { files: number; added: number; deleted: number; ahead: number };
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
  const q = useChanges(path);
  const t = q.data?.total;
  if (!t || (!t.files && !t.ahead)) return null;
  return (
    <Link to={`/node/${path}?tab=worktree`} title="code that differs from the base (see the worktree tab)">
      <Badge kind="warn">
        code changed: {t.files} file{t.files === 1 ? "" : "s"}, +{t.added} −{t.deleted}
        {t.ahead ? `, ${t.ahead} commit${t.ahead === 1 ? "" : "s"} ahead` : ""}
      </Badge>
    </Link>
  );
}

function DiffView({ text }: { text: string }) {
  return (
    <pre className="diff">
      {text.split("\n").map((line, i) => (
        <div key={i} className={line.startsWith("+++") || line.startsWith("---") ? "meta" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : ""}>
          {line || " "}
        </div>
      ))}
    </pre>
  );
}

function FileDiff({ path, sub, file }: { path: string; sub: string; file: string }) {
  const q = useQuery({
    queryKey: ["wt-diff", path, sub, file],
    queryFn: () => api.get<{ diff: string; truncated: boolean }>("/worktrees/diff", { ref: path, sub, path: file }),
  });
  if (q.isLoading) return <Loading what="diff" />;
  if (q.error) return <ErrorBox error={q.error} />;
  return (
    <div>
      {q.data!.truncated && <div className="notice">diff truncated</div>}
      {q.data!.diff ? <DiffView text={q.data!.diff} /> : <div className="muted small">no textual diff (binary or mode change)</div>}
    </div>
  );
}

function RepoChangesView({ path, sub, r }: { path: string; sub: string; r: RepoChanges }) {
  const [open, setOpen] = useState<string | null>(null);
  const title = sub || "worktree";
  if (!r.exists) return <div className="notice">{title}: {r.path} is missing on disk</div>;
  if (r.error) return <div className="notice">{title}: {r.error}</div>;
  const files = r.files ?? [];
  const untracked = r.untracked ?? [];
  const toggle = (f: string) => setOpen(open === f ? null : f);
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>
        {title} <span className="muted small mono">vs {r.base} (merge-base {r.merge_base})</span>
      </h3>
      <div className="muted small">
        {r.ahead ?? 0} commit(s) ahead, {r.behind ?? 0} behind · {files.length} file(s) changed, +{r.added ?? 0} −{r.deleted ?? 0}
        {untracked.length > 0 && ` · ${untracked.length} untracked`}
      </div>
      {!files.length && !untracked.length && !r.ahead && <div className="muted">no changes</div>}
      {(r.commits ?? []).length > 0 && (
        <table className="small" style={{ marginTop: 8 }}>
          <tbody>
            {r.commits!.map((c) => (
              <tr key={c.sha}>
                <td className="mono">{c.sha}</td>
                <td>{c.subject}</td>
                <td className="muted">{c.author}</td>
                <td className="muted">{c.when}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {(files.length > 0 || untracked.length > 0) && (
        <table className="small" style={{ marginTop: 8 }}>
          <thead>
            <tr>
              <th>st</th>
              <th className="num">+</th>
              <th className="num">−</th>
              <th>file</th>
            </tr>
          </thead>
          <tbody>
            {files.map((f) => [
              <tr key={f.path} className={`clickable ${open === f.path ? "selected" : ""}`} onClick={() => toggle(f.path)}>
                <td className="mono">{f.status}</td>
                <td className="num diff-add">{f.added ?? "bin"}</td>
                <td className="num diff-del">{f.deleted ?? "bin"}</td>
                <td className="mono">
                  {f.old_path ? `${f.old_path} → ` : ""}
                  {f.path} {f.uncommitted && <Badge kind="warn">uncommitted</Badge>}
                </td>
              </tr>,
              open === f.path && (
                <tr key={`${f.path}:diff`}>
                  <td colSpan={4}>
                    <FileDiff path={path} sub={sub} file={f.path} />
                  </td>
                </tr>
              ),
            ])}
            {untracked.map((u) => [
              <tr key={`?${u}`} className={`clickable ${open === u ? "selected" : ""}`} onClick={() => toggle(u)}>
                <td className="mono">?</td>
                <td />
                <td />
                <td className="mono">
                  {u} <Badge>untracked</Badge>
                </td>
              </tr>,
              open === u && (
                <tr key={`?${u}:diff`}>
                  <td colSpan={4}>
                    <FileDiff path={path} sub={sub} file={u} />
                  </td>
                </tr>
              ),
            ])}
          </tbody>
        </table>
      )}
      {r.truncated && <div className="muted small">file list truncated</div>}
    </div>
  );
}

function ChangesSection({ path }: { path: string }) {
  const q = useChanges(path);
  if (q.isLoading) return <Loading what="changes" />;
  if (q.error) return <ErrorBox error={q.error} />;
  const repos = Object.entries(q.data?.repos ?? {});
  return (
    <div>
      <h2>Changes vs base</h2>
      {repos.map(([sub, r]) => (
        <RepoChangesView key={sub || "."} path={path} sub={sub} r={r} />
      ))}
    </div>
  );
}

function NotesTab({ ctx }: { ctx: NodeContext }) {
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
          <button onClick={() => setEditing(true)}>Edit</button>
          <span className="muted small">Markdown, stored in the node's node.yaml · featx node update {ctx.node.path} --notes …</span>
        </div>
        {ctx.notes ? <Markdown text={ctx.notes} /> : <div className="muted">No notes yet — Edit to add.</div>}
      </div>
    );
  }
  return (
    <div>
      <div className="grid2">
        <div>
          <textarea className="mono" rows={22} value={text} aria-label="notes" autoFocus onChange={(e) => setText(e.target.value)} placeholder="# Notes&#10;&#10;Markdown" />
        </div>
        <div className="card" style={{ overflow: "auto", maxHeight: "60vh" }}>
          {text.trim() ? <Markdown text={text} /> : <span className="muted">preview</span>}
        </div>
      </div>
      <div className="row" style={{ marginTop: 6 }}>
        <button className="primary" disabled={act.busy || text === (ctx.notes ?? "")} onClick={save}>
          Save notes
        </button>
        <button
          onClick={() => {
            setText(ctx.notes ?? "");
            setEditing(false);
          }}
        >
          Cancel
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
  const act = useAction();
  const invalidate = useInvalidateNode();
  const [hyp, setHyp] = useState(n.hypothesis);
  const [note, setNote] = useState(n.verdict_note);
  const [comment, setComment] = useState(n.comment ?? "");
  const [dialog, setDialog] = useState<"add" | "mv" | "rm" | null>(null);
  useEffect(() => {
    setHyp(n.hypothesis);
    setNote(n.verdict_note);
    setComment(n.comment ?? "");
  }, [n.hypothesis, n.verdict_note, n.comment]);
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
        <h1 style={{ margin: 0 }}>
          <VerdictMark verdict={n.verdict} /> <span className="mono">{n.path}</span>
        </h1>
        <Badge>{n.kind}</Badge>
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
      <div className="card form" style={{ marginTop: 10 }}>
        <label>{t("hypothesis")}</label>
        <div className="row">
          <textarea rows={2} style={{ flex: 1 }} value={hyp} onChange={(e) => setHyp(e.target.value)} />
          <button disabled={hyp === n.hypothesis || act.busy} onClick={() => update({ hypothesis: hyp })}>
            Save
          </button>
        </div>
        <label>{t("verdict")}</label>
        <div className="row">
          <select value={n.verdict} onChange={(e) => update({ verdict: e.target.value })}>
            {VERDICTS.map((v) => (
              <option key={v}>{v}</option>
            ))}
          </select>
          <input type="text" style={{ flex: 1 }} value={note} placeholder={t("verdict note")} onChange={(e) => setNote(e.target.value)} />
          <button disabled={note === n.verdict_note || act.busy} onClick={() => update({ verdict_note: note })}>
            {t("Save note")}
          </button>
          <label>status</label>
          <select value={n.status} onChange={(e) => update({ status: e.target.value })}>
            {STATUSES.map((v) => (
              <option key={v}>{v}</option>
            ))}
          </select>
        </div>
        <label>{t("evidence")}</label>
        <EvidenceList evidence={n.evidence} busy={act.busy} save={(list) => update({ evidence: list })} add={(ev) => update({ add_evidence: [ev] })} />
        <label>{t("comment")}</label>
        <div className="row" style={{ alignItems: "flex-start" }}>
          <textarea rows={3} style={{ flex: 1, fontFamily: "inherit", fontSize: 13 }} value={comment} placeholder={t("your thoughts on this node: ideas, doubts, what to try next")} onChange={(e) => setComment(e.target.value)} />
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <button disabled={comment === (n.comment ?? "") || act.busy} onClick={() => update({ comment })}>
              Save
            </button>
            {comment !== (n.comment ?? "") && <span className="muted small">unsaved</span>}
          </div>
        </div>
      </div>
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
  const tab = (TABS as readonly string[]).includes(sp.get("tab") ?? "") ? (sp.get("tab") as Tab) : "runsets";
  const q = useQuery({
    queryKey: ["node", path],
    queryFn: () => api.get<NodeContext>("/nodes/context", { ref: path, max_runsets: 50 }),
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
      {tab === "runsets" && <RunsetsTab ctx={ctx} />}
      {tab === "submit" && <SubmitTab ctx={ctx} />}
      {tab === "recipes" && <RecipesTab ctx={ctx} />}
      {tab === "scripts" && <ScriptsTab key={`scripts:${ctx.node.id}`} ctx={ctx} />}
      {tab === "worktree" && <WorktreeTab ctx={ctx} />}
      {tab === "notes" && <NotesTab key={`notes:${ctx.node.id}`} ctx={ctx} />}
      {ctx.children.length > 0 && (
        <>
          <h2>{t("Children")}</h2>
          <ul>
            {ctx.children.map((c) => (
              <li key={c.path}>
                <VerdictMark verdict={c.verdict} /> <Link to={`/node/${c.path}`}>{c.path}</Link> <span className="muted">{c.hypothesis}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
