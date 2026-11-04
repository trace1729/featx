import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import YAML from "yaml";
import { api } from "../api";
import { ChangesView, DiffView } from "../components/Changes";
import { FileBrowser, FileView, Markdown } from "../components/FileViewer";
import { LogTail } from "../components/LogTail";
import { Badge, ErrorBox, Eta, Json, Loading, Progress, StateBadge, Tabs, useAction } from "../components/ui";
import { YamlEditor } from "../components/YamlEditor";
import type { CodeView, JobView, RecordRepo, RunsetSummary, TaskView } from "../types";
import { countsText, fmtCount, fmtTime, fmtValue, overrideKeys, splitRunset } from "../util";
import { PriorityControl } from "./Tasks";
import { useI18n } from "../i18n";

const ACTIVE = ["queued", "running", "launching"];
const TABS = ["jobs", "code", "results", "details"] as const;
type Tab = (typeof TABS)[number];
const RUNSET_TAB_ALIASES: Record<string, Tab> = { post: "results", files: "results", "problem log": "details", provenance: "details" };

function jobPct(job: JobView, target?: number): number | null {
  if (job.state === "succeeded") return 100;
  if (!target || job.progress?.inst === undefined) return null;
  return (100 * job.progress.inst) / target;
}

const JOB_STATES = ["running", "queued", "succeeded", "failed", "cancelled"];

/** State chips with counts above the jobs table ("all" clears the filter); kept in ?jobs=. */
function JobFilter({ jobs, selected, onChange }: { jobs: JobView[]; selected: string[]; onChange: (next: string[]) => void }) {
  const { t } = useI18n();
  const counts: Record<string, number> = {};
  for (const j of jobs) counts[j.state] = (counts[j.state] ?? 0) + 1;
  const states = [...JOB_STATES];
  for (const s of [...Object.keys(counts), ...selected]) if (!states.includes(s)) states.push(s);
  return (
    <div className="chips" style={{ marginBottom: 6 }} role="group" aria-label={t("filter jobs by state")}>
      <button className={`chip ${selected.length ? "" : "active"}`} aria-pressed={!selected.length} onClick={() => onChange([])}>
        {t("all")} {jobs.length}
      </button>
      {states.map((s) => (
        <button
          key={s}
          className={`chip ${selected.includes(s) ? "active" : ""}`}
          aria-pressed={selected.includes(s)}
          disabled={!counts[s] && !selected.includes(s)}
          onClick={() => onChange(selected.includes(s) ? selected.filter((x) => x !== s) : [...selected, s])}
        >
          <StateBadge state={s} /> {counts[s] ?? 0}
        </button>
      ))}
    </div>
  );
}

function JobsTable({ task, job, onJob, states }: { task: TaskView; job: string | null; onJob: (j: string) => void; states?: string[] }) {
  const { t } = useI18n();
  const all = task.jobs ?? [];
  const jobs = states && states.length ? all.filter((j) => states.includes(j.state)) : all;
  const wrap = useRef<HTMLDivElement>(null);
  // A campaign has ~1000 jobs: the table scrolls in its own box (header kept visible) so the
  // log below stays reachable; keep the selected job (e.g. from ?job=) in view without
  // scrolling the page.
  useEffect(() => {
    const box = wrap.current;
    const row = box?.querySelector<HTMLTableRowElement>("tr.selected");
    if (!box || !row) return;
    const head = box.querySelector("thead")?.offsetHeight ?? 0;
    if (row.offsetTop - head < box.scrollTop || row.offsetTop + row.offsetHeight > box.scrollTop + box.clientHeight) {
      box.scrollTop = row.offsetTop - head - (box.clientHeight - head) / 2;
    }
  }, [job, jobs.length]);
  if (!all.length) return <div className="muted">{t("this task has no per-point jobs.")}</div>;
  if (!jobs.length) return <div className="muted">{t("no job matches the state filter.")}</div>;
  return (
    <div className="table-wrap jobs-wrap" ref={wrap} data-testid="jobs-scroll">
      <table>
        <thead>
          <tr>
            <th>{t("job")}</th>
            <th>{t("state")}</th>
            <th>{t("host")}</th>
            <th className="num">{t("attempt")}</th>
            <th style={{ width: "22%" }}>{t("progress")}</th>
            <th>ETA</th>
            <th className="num">rc</th>
            <th>{t("failure")}</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.name} className={`clickable ${j.name === job ? "selected" : ""}`} onClick={() => onJob(j.name)}>
              <td className="mono">{j.name}</td>
              <td>
                <StateBadge state={j.state} />
              </td>
              <td className="mono">{j.host ?? ""}</td>
              <td className="num">{j.attempt}</td>
              <td>
                {j.state === "running" || j.state === "succeeded" ? (
                  <Progress
                    pct={jobPct(j, task.target_inst)}
                    label={
                      j.progress?.inst !== undefined
                        ? `${fmtCount(j.progress.inst)}${task.target_inst ? ` / ${fmtCount(task.target_inst)}` : ""}${j.progress.inst_per_s ? ` · ${fmtCount(j.progress.inst_per_s)}/s` : ""}`
                        : undefined
                    }
                  />
                ) : (
                  ""
                )}
              </td>
              <td>{ACTIVE.includes(j.state) ? <Eta seconds={j.eta_s} /> : ""}</td>
              <td className="num">{j.rc ?? ""}</td>
              <td className="small">
                {j.failure && (
                  <span title={j.failure.tail ?? ""}>
                    <Badge kind={j.failure.class === "design" ? "failed" : "warn"}>{t(j.failure.class ?? "?")}</Badge> <span className="mono">{j.failure.signature}</span>
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TaskActions({ task, rsRef }: { task: TaskView; rsRef: string }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const act = useAction();
  const [msg, setMsg] = useState<string | null>(null);
  const call = (what: string, body: Record<string, unknown> = {}) =>
    act.run(async () => {
      setMsg(null);
      const r = await api.post<Record<string, unknown>>(`/tasks/${task.id}/${what}`, body);
      setMsg(`${what}: ${JSON.stringify(r)}`);
      await qc.invalidateQueries();
    });
  const canRelaunch = (task.jobs ?? []).some((j) => j.state === "failed" || j.state === "cancelled") || task.state === "failed" || task.state === "cancelled";
  return (
    <div>
      <div className="row">
        <button disabled={!ACTIVE.includes(task.state) || act.busy} onClick={() => call("cancel")}>
          {t("Cancel")}
        </button>
        <button disabled={!canRelaunch || act.busy} onClick={() => call("relaunch")} title={t("re-queue failed/cancelled jobs in this runset")}>
          {t("Relaunch failed")}
        </button>
        <button disabled={ACTIVE.includes(task.state) || act.busy} onClick={() => call("resubmit")} title={t("new derived runset with re-resolved recipes")}>
          {t("Resubmit…")}
        </button>
        {task.blocked && (
          <button className="danger" disabled={act.busy} onClick={() => call("unblock", { extra: 1 })}>
            {t("Unblock (+1 design relaunch)")}
          </button>
        )}
        {ACTIVE.includes(task.state) && (
          <>
            <span className="muted small">{t("priority")}</span>
            <PriorityControl task={task} />
          </>
        )}
        <span className="muted small">
          {t("design relaunches {count} · featx task relaunch {task}", { count: task.design_relaunches, task: task.id })}
        </span>
      </div>
      {act.error ? (
        <div className="error">
          {act.error instanceof Error ? act.error.message : String(act.error)}
          {String(act.error).includes("problem-analysis") && (
            <div>
              {t("Use the problem log tab to record what went wrong and how it is fixed, then relaunch. ({rsRef})", { rsRef })}
            </div>
          )}
        </div>
      ) : (
        msg && <div className="okbox mono small">{msg}</div>
      )}
    </div>
  );
}

function PostTab({ rs }: { rs: RunsetSummary }) {
  const { t } = useI18n();
  const steps = Object.keys({ ...rs.post, ...(rs.reports ?? {}) });
  if (!steps.length) return <div className="muted">{t("no post-processing output yet.")}</div>;
  return (
    <div>
      {steps.map((step) => (
        <div key={step} className="card" style={{ marginBottom: 12 }}>
          <h3>post/{step}</h3>
          {rs.post[step] && (
            <table style={{ width: "auto" }}>
              <tbody>
                {Object.entries(rs.post[step]).map(([k, v]) => (
                  <tr key={k}>
                    <td className="mono">{k}</td>
                    <td className="num mono">{fmtValue(v)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {rs.reports?.[step] && <Markdown text={rs.reports[step]} />}
        </div>
      ))}
    </div>
  );
}

function ProblemLogTab({ rs, task }: { rs: RunsetSummary; task?: TaskView }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const act = useAction();
  const [problem, setProblem] = useState("");
  const [solution, setSolution] = useState("");
  const [code, setCode] = useState("");
  const [n, setN] = useState(0);
  const path = `${rs.dir}/problem-analysis.log`;
  const append = () =>
    act.run(async () => {
      await api.post("/problem-log", { target: rs.ref, problem, solution, code, task: task?.id, author: "ui" });
      setProblem("");
      setSolution("");
      setCode("");
      setN(n + 1);
      await qc.invalidateQueries({ queryKey: ["file", path] });
    });
  return (
    <div>
      {rs.problem_log_tail ? <FileView key={n} path={path} /> : <div className="muted">{t("no entries yet.")}</div>}
      <h3>{t("Append an entry")}</h3>
      <p className="muted small">{t("A design failure can only be relaunched after an entry newer than the failure (featx log append {ref} -p … -s …).", { ref: rs.ref })}</p>
      <div className="form">
        <label>{t("problem")}</label>
        <textarea rows={3} value={problem} onChange={(e) => setProblem(e.target.value)} placeholder={t("what failed and the evidence for the suspected cause")} />
        <label>{t("solution")}</label>
        <textarea rows={3} value={solution} onChange={(e) => setSolution(e.target.value)} placeholder={t("what was changed")} />
        <label>{t("code")}</label>
        <textarea rows={3} value={code} onChange={(e) => setCode(e.target.value)} placeholder={t("how the code change solves it (optional)")} />
        <span />
        <div>
          <button className="primary" disabled={!problem.trim() || !solution.trim() || act.busy} onClick={append}>
            {t("Append")}
          </button>
          <ErrorBox error={act.error} />
        </div>
      </div>
    </div>
  );
}

function paramText(v: unknown): string {
  if (Array.isArray(v)) return v.map(paramText).join(", ");
  if (typeof v === "object" && v !== null) return Object.entries(v).map(([k, x]) => `${k}=${paramText(x)}`).join(" ");
  return fmtValue(v);
}

/** The selected stage's headline parameters (the --set overrides marked) and, on demand, that
 * stage's full resolved recipe. A run without a build of its own names the binary it ran. */
function StageParams({ rs, stage, hasBuild }: { rs: RunsetSummary; stage: string; hasBuild: boolean }) {
  const { t } = useI18n();
  const [full, setFull] = useState(false);
  const overrides = [...overrideKeys(rs.overrides)].map((k) => k.replace(/^post\.steps\./, "post.")).filter((k) => k.startsWith(`${stage}.`));
  const items: [string, unknown][] = Object.entries(rs.params?.[stage] ?? {});
  if (stage === "run" && !hasBuild && rs.code) {
    if (rs.code.from) items.unshift(["binary from", rs.code.from]);
    else if (rs.code.bin_key || rs.code.bin_emu) items.unshift([rs.code.imported ? "imported binary" : "binary", rs.code.bin_key || rs.code.bin_emu]);
  }
  const recipe = rs.recipes?.[stage];
  return (
    <div className="stage-params" data-testid="stage-params">
      <div className="row">
        <b className="small">
          {stage} {t("parameters")}
        </b>
        {overrides.length > 0 && (
          <Badge kind="warn" title={overrides.join(", ")}>
            {t("overrides")}: {overrides.length}
          </Badge>
        )}
        <span className="spacer" />
        {recipe && (
          <button className="link small" onClick={() => setFull(!full)}>
            {full ? t("hide full recipe") : t("full recipe (YAML)")}
          </button>
        )}
      </div>
      {full ? (
        <YamlEditor value={YAML.stringify(recipe ?? {})} readOnly height="360px" />
      ) : items.length === 0 ? (
        <div className="muted small">—</div>
      ) : (
        <div className="param-grid small">
          {items.map(([key, value]) => (
            <div key={key} className={key.length + paramText(value).length > 34 ? "wide" : undefined}>
              <span className="muted">{key}</span> <span className="mono">{paramText(value)}</span>{" "}
              {overrides.includes(`${stage}.${key}`) && <Badge kind="warn">{t("set")}</Badge>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** What the code record holds per repo: the HEAD the build compiled and the uncommitted changes
 * saved with it (untracked files saved / left out). Clean repos are listed on one line. */
function RecordTable({ record, legacy }: { record: Record<string, RecordRepo>; legacy: boolean }) {
  const { t } = useI18n();
  const rows = Object.entries(record);
  const shown = rows.filter(([name, r]) => !name || r.dirty || (r.omitted ?? []).length || r.unsaved_untracked);
  const clean = rows.filter((row) => !shown.includes(row));
  return (
    <div className="card" data-testid="code-record">
      <b className="small">{legacy ? t("recorded at submit") : t("code record")}</b>
      <table className="small record-table">
        <thead>
          <tr>
            <th>{t("repo")}</th>
            <th>{t("branch")}</th>
            <th>HEAD</th>
            <th>{t("uncommitted")}</th>
            <th>{t("untracked")}</th>
          </tr>
        </thead>
        <tbody>
          {shown.map(([name, r]) => {
            const omitted = r.omitted ?? [];
            return (
              <tr key={name || "."}>
                <td className="mono">
                  {name || t("superproject")}
                  {r.shared ? <span className="muted"> ({t("shared")})</span> : null}
                </td>
                <td className="mono muted">{r.branch ?? ""}</td>
                <td className="mono" title={r.head ?? undefined}>{(r.head ?? "").slice(0, 12)}</td>
                <td>
                  {r.patch ? (
                    <span className="badge warn">
                      {r.files ?? 0} {t("files")} <span className="diff-add">+{r.added ?? 0}</span> <span className="diff-del">−{r.deleted ?? 0}</span>
                    </span>
                  ) : r.dirty ? (
                    <span className="badge failed">{t("dirty, not saved")}</span>
                  ) : (
                    <span className="muted">{t("clean")}</span>
                  )}
                </td>
                <td className="small">
                  {r.untracked ? <span>{r.untracked} {t("saved")}</span> : null}
                  {omitted.length > 0 && (
                    <span className="muted" title={omitted.map((o) => `${o.path}${o.files ? ` (${o.files}+ files)` : ""}: ${o.reason}`).join("\n")}>
                      {r.untracked ? " · " : ""}
                      {omitted.length} {t("not saved")}: <span className="mono">{omitted.slice(0, 2).map((o) => o.path).join(", ")}{omitted.length > 2 ? ", …" : ""}</span>
                    </span>
                  )}
                  {r.unsaved_untracked ? <span className="muted">{r.unsaved_untracked} {t("untracked (not saved)")}</span> : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {clean.length > 0 && (
        <div className="muted small">
          {t("clean")} ({clean.length}): <span className="mono">{clean.map(([name, r]) => `${name}@${(r.head ?? "").slice(0, 8)}`).join(", ")}</span>
        </div>
      )}
    </div>
  );
}

/** What a diff mode shows, as one hint line under the selector. */
const AGAINST_HINT: Record<string, string> = {
  base: "merge-base with the node base branch → this runset's code: what this runset adds",
  prev: "previous runset → this runset: what changed since it",
  runset: "the chosen runset → this runset",
  worktree: "this runset → the worktree now: what changed since this runset ran",
};

function CodeTab({ rs }: { rs: RunsetSummary }) {
  const { t } = useI18n();
  const [sp, setSp] = useSearchParams();
  const against = sp.get("against") || "base";
  const node = splitRunset(rs.ref).node;
  const setAgainst = (value: string) => {
    const next = new URLSearchParams(sp);
    if (value && value !== "base") next.set("against", value);
    else next.delete("against");
    setSp(next, { replace: true });
  };
  const runsets = useQuery({
    queryKey: ["runsets", node],
    queryFn: () => api.get<RunsetSummary[]>("/runsets", { ref: node }),
    staleTime: 30000,
  });
  const q = useQuery({
    queryKey: ["code", rs.ref, against],
    queryFn: () => api.get<CodeView>("/runsets/code", { ref: rs.ref, against }),
  });
  const choices = (runsets.data ?? []).filter((r) => r.ref !== rs.ref);
  const known = ["base", "prev", "worktree"].includes(against) || choices.some((r) => r.ref === against);
  return (
    <div>
      <div className="row" style={{ marginBottom: 8 }}>
        <span className="muted small">{t("against")}</span>
        <select value={against} onChange={(e) => setAgainst(e.target.value)} aria-label={t("against")}>
          <option value="base">{t("node base")}: {t("what this runset adds")}</option>
          <option value="prev">{t("previous runset")}: {t("what changed since it")}</option>
          <option value="worktree">{t("worktree now")}: {t("changes since this runset")}</option>
          {choices.map((r) => (
            <option key={r.ref} value={r.ref}>
              {r.ref.slice(node.length + 1)} → {t("this runset")}
            </option>
          ))}
          {!known && <option value={against}>{against}</option>}
        </select>
        {q.data?.compared_runset && <span className="muted small">{t("vs")} {q.data.compared_runset}</span>}
        {q.data?.source_runset && (
          <span className="muted small">
            {t("code from runset")} <Link to={`/runset/${q.data.source_runset}?tab=code`}>{q.data.source_runset}</Link>
          </span>
        )}
      </div>
      {q.data && !q.data.no_code && q.data.against && <p className="muted small" data-testid="against-hint">{t(AGAINST_HINT[q.data.against] ?? "")}</p>}
      {q.data?.drift && (
        <div className="notice">
          {t("code drift")}: {q.data.drift} — {t("the record is the code at build launch; the binary may not match it")}
        </div>
      )}
      {q.isLoading ? (
        <Loading what="code" />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : q.data?.no_code ? (
        <div className="notice">
          {t("no code recorded")}: {q.data.reason}
        </div>
      ) : q.data ? (
        <>
          {q.data.legacy && (
            <p className="notice small" data-testid="legacy-note">
              {t("legacy runset")}: {t("from before code records: the HEADs recorded at submit plus the tracked changes saved in src-patches; untracked files were not saved.")}
            </p>
          )}
          {!q.data.legacy && q.data.compared_legacy && (
            <p className="notice small">{t("the compared runset is a legacy runset: only its HEADs and src-patches are compared.")}</p>
          )}
          {q.data.record && Object.keys(q.data.record).length > 0 && <RecordTable record={q.data.record} legacy={q.data.legacy} />}
          {Object.keys(q.data.repos ?? {}).length > 0 && (
            <ChangesView
              changes={q.data}
              scope={`code:${rs.ref}:${against}`}
              rootLabel={t("superproject")}
              vs={q.data.compared_runset ? q.data.compared_runset.split("/").pop() : q.data.against === "worktree" ? t("worktree now") : undefined}
              fetchDiff={(sub, path) => api.get("/runsets/code/diff", { ref: rs.ref, against, sub, path })}
            />
          )}
          {q.data.patches && (
            <div data-testid="raw-patches">
              {Object.entries(q.data.patches).map(([name, text]) => (
                <div key={name} className="card">
                  <h3 className="mono">{name}</h3>
                  <DiffView text={text} />
                </div>
              ))}
              {!Object.keys(q.data.patches).length && <div className="muted small">{t("no saved patches")}</div>}
            </div>
          )}
        </>
      ) : null}
    </div>
  );
}

function ArchiveButton({ rs }: { rs: RunsetSummary }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const act = useAction();
  return (
    <button
      title={rs.archive_note || undefined}
      disabled={act.busy}
      onClick={() =>
        act.run(async () => {
          await api.post("/runsets/archive", { ref: rs.ref, archived: !rs.archived });
          await qc.invalidateQueries();
        })
      }
    >
      {rs.archived ? t("unarchive") : t("archive")}
    </button>
  );
}

export function RunsetPage() {
  const { t } = useI18n();
  const ref = useParams()["*"] ?? "";
  const { node, name } = splitRunset(ref);
  const [sp, setSp] = useSearchParams();
  const setParam = (k: string, v: string | null) => {
    const next = new URLSearchParams(sp);
    if (v) next.set(k, v);
    else next.delete(k);
    setSp(next, { replace: true });
  };
  const requestedTab = sp.get("tab") ?? "";
  const tab = (TABS as readonly string[]).includes(requestedTab) ? (requestedTab as Tab) : RUNSET_TAB_ALIASES[requestedTab] ?? "jobs";
  const jobStates = (sp.get("jobs") ?? "").split(",").filter(Boolean);
  const rsq = useQuery({ queryKey: ["result", ref], queryFn: () => api.get<RunsetSummary>("/result", { ref }), refetchInterval: 5000 });
  const briefs = rsq.data?.tasks ?? [];
  const tq = useQueries({
    queries: briefs.map((b) => ({
      queryKey: ["task", b.id],
      queryFn: () => api.get<TaskView>(`/tasks/${b.id}`),
      refetchInterval: ACTIVE.includes(b.state) ? 3000 : false,
    })),
  });
  if (rsq.isLoading) return <Loading what={ref} />;
  if (rsq.error) return <ErrorBox error={rsq.error} />;
  const rs = rsq.data!;
  const tasks = tq.map((q) => q.data).filter((t): t is TaskView => !!t);
  const pick =
    tasks.find((t) => t.id === sp.get("task")) ??
    tasks.find((t) => t.state === "running") ??
    tasks.find((t) => t.state === "failed") ??
    tasks.find((t) => t.kind === "run") ??
    tasks[0];
  const jobs = pick?.jobs ?? [];
  const shownJobs = jobStates.length ? jobs.filter((j) => jobStates.includes(j.state)) : jobs;
  const job =
    sp.get("job") && shownJobs.some((j) => j.name === sp.get("job"))
      ? sp.get("job")
      : (shownJobs.find((j) => j.state === "running") ?? shownJobs.find((j) => j.state === "failed") ?? shownJobs[0])?.name ?? null;
  return (
    <div>
      <div className="crumbs">
        <Link to={`/node/${node}`}>{node}</Link> /
      </div>
      <div className="row">
        <h1 style={{ margin: 0 }} className="mono">
          {name}
        </h1>
        {rs.sealed && <Badge kind="sealed">{t("sealed")}</Badge>}
        {rs.archived && <Badge kind="warn">{t("archived")}</Badge>}
        <span>{rs.purpose}</span>
        <span className="spacer" />
        <span className="muted">{fmtTime(rs.created)}</span>
        <ArchiveButton rs={rs} />
      </div>
      {rs.derived_from && (
        <p className="muted">
          {t("derived from")} <Link to={`/runset/${rs.derived_from}`}>{rs.derived_from}</Link>
        </p>
      )}
      <div className="row" style={{ margin: "10px 0" }}>
        {tasks.map((task, i) => (
          <span key={task.id} className="row" style={{ gap: 4 }}>
            {i > 0 && <span className="muted">→</span>}
            <button className={`chip ${task.id === pick?.id ? "active" : ""}`} onClick={() => setSp({ task: task.id }, { replace: true })}>
              <b>{t(task.kind)}</b> <StateBadge state={task.state} />
              {task.state === "running" && task.progress_pct != null && <span className="small">{task.progress_pct.toFixed(0)}%</span>}
              {ACTIVE.includes(task.state) && task.eta_s != null && (
                <span className="small muted">
                  <Eta seconds={task.eta_s} />
                </span>
              )}
              {task.blocked && <StateBadge state="blocked" />}
            </button>
          </span>
        ))}
      </div>
      {pick && (
        <div className="card" style={{ marginBottom: 10 }}>
          <div className="row">
            <b>{t(pick.kind)}</b> <span className="mono small">{pick.id}</span> <span>{countsText(pick.counts, t)}</span> <span className="muted">{pick.note}</span>
          </div>
          {pick.failure != null && <pre className="error mono small">{typeof pick.failure === "string" ? pick.failure : JSON.stringify(pick.failure, null, 2)}</pre>}
          <TaskActions task={pick} rsRef={ref} />
          <StageParams key={pick.kind} rs={rs} stage={pick.kind} hasBuild={tasks.some((x) => x.kind === "build")} />
        </div>
      )}
      <Tabs tabs={TABS} value={tab} onChange={(t) => setParam("tab", t)} />
      {tab === "jobs" && pick && (
        <div>
          <JobFilter jobs={jobs} selected={jobStates} onChange={(next) => setParam("jobs", next.join(",") || null)} />
          <JobsTable task={pick} job={job} onJob={(j) => setParam("job", j)} states={jobStates} />
          <h3>
            {t("Log")} {job ? <span className="mono">{job}</span> : `(${pick.kind})`}
          </h3>
          <LogTail
            key={`${pick.id}/${job}`}
            task={pick.id}
            job={job ?? undefined}
            live={ACTIVE.includes(pick.state)}
            initialStream={jobs.find((j) => j.name === job)?.state === "failed" ? "err" : "out"}
          />
        </div>
      )}
      {tab === "code" && <CodeTab rs={rs} />}
      {tab === "results" && (
        <div>
          <section className="tab-section"><h2>{t("post")}</h2><PostTab rs={rs} /></section>
          <section className="tab-section"><h2>{t("files")}</h2><FileBrowser dir={rs.dir} depth={3} /></section>
        </div>
      )}
      {tab === "details" && (
        <div>
          <section className="tab-section"><h2>{t("problem log")}</h2><ProblemLogTab rs={rs} task={pick} /></section>
          <section className="tab-section">
            <h2>{t("provenance")}</h2>
            <h3>{t("binary")}</h3>
            <Json value={rs.bin ?? null} />
            <Json value={rs.provenance ?? {}} />
            <p className="muted mono small">{rs.dir}</p>
          </section>
        </div>
      )}
    </div>
  );
}
