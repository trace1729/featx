import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { FileBrowser, FileView, Markdown } from "../components/FileViewer";
import { LogTail } from "../components/LogTail";
import { Badge, ErrorBox, Eta, Json, Loading, Progress, StateBadge, Tabs, useAction } from "../components/ui";
import type { JobView, RunsetSummary, TaskView } from "../types";
import { countsText, fmtCount, fmtTime, fmtValue, splitRunset } from "../util";
import { PriorityControl } from "./Tasks";
import { useI18n } from "../i18n";

const ACTIVE = ["queued", "running", "launching"];
const TABS = ["jobs", "post", "files", "problem log", "provenance"] as const;
type Tab = (typeof TABS)[number];

function jobPct(job: JobView, target?: number): number | null {
  if (job.state === "succeeded") return 100;
  if (!target || job.progress?.inst === undefined) return null;
  return (100 * job.progress.inst) / target;
}

function JobsTable({ task, job, onJob }: { task: TaskView; job: string | null; onJob: (j: string) => void }) {
  const { t } = useI18n();
  const jobs = task.jobs ?? [];
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
  if (!jobs.length) return <div className="muted">{t("this task has no per-point jobs.")}</div>;
  return (
    <div className="table-wrap jobs-wrap" ref={wrap} data-testid="jobs-scroll">
      <table>
        <thead>
          <tr>
            <th>job</th>
            <th>state</th>
            <th>host</th>
            <th className="num">attempt</th>
            <th style={{ width: "22%" }}>progress</th>
            <th>ETA</th>
            <th className="num">rc</th>
            <th>failure</th>
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
                    <Badge kind={j.failure.class === "design" ? "failed" : "warn"}>{j.failure.class ?? "?"}</Badge> <span className="mono">{j.failure.signature}</span>
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
        <button disabled={!canRelaunch || act.busy} onClick={() => call("relaunch")} title="re-queue failed/cancelled jobs in this runset">
          {t("Relaunch failed")}
        </button>
        <button disabled={ACTIVE.includes(task.state) || act.busy} onClick={() => call("resubmit")} title="new derived runset with re-resolved recipes">
          {t("Resubmit…")}
        </button>
        {task.blocked && (
          <button className="danger" disabled={act.busy} onClick={() => call("unblock", { extra: 1 })}>
            Unblock (+1 design relaunch)
          </button>
        )}
        {ACTIVE.includes(task.state) && (
          <>
            <span className="muted small">priority</span>
            <PriorityControl task={task} />
          </>
        )}
        <span className="muted small">
          design relaunches {task.design_relaunches} · featx task relaunch {task.id}
        </span>
      </div>
      {act.error ? (
        <div className="error">
          {act.error instanceof Error ? act.error.message : String(act.error)}
          {String(act.error).includes("problem-analysis") && (
            <div>
              Use the <b>problem log</b> tab to record what went wrong and how it is fixed, then relaunch. ({rsRef})
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
  const steps = Object.keys({ ...rs.post, ...(rs.reports ?? {}) });
  if (!steps.length) return <div className="muted">no post-processing output yet.</div>;
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
      {rs.problem_log_tail ? <FileView key={n} path={path} /> : <div className="muted">no entries yet.</div>}
      <h3>{t("Append an entry")}</h3>
      <p className="muted small">A design failure can only be relaunched after an entry newer than the failure (featx log append {rs.ref} -p … -s …).</p>
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

export function RunsetPage() {
  const ref = useParams()["*"] ?? "";
  const { node, name } = splitRunset(ref);
  const [sp, setSp] = useSearchParams();
  const setParam = (k: string, v: string | null) => {
    const next = new URLSearchParams(sp);
    if (v) next.set(k, v);
    else next.delete(k);
    setSp(next, { replace: true });
  };
  const tab = (TABS as readonly string[]).includes(sp.get("tab") ?? "") ? (sp.get("tab") as Tab) : "jobs";
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
  const job =
    sp.get("job") && jobs.some((j) => j.name === sp.get("job"))
      ? sp.get("job")
      : (jobs.find((j) => j.state === "running") ?? jobs.find((j) => j.state === "failed") ?? jobs[0])?.name ?? null;
  return (
    <div>
      <div className="crumbs">
        <Link to={`/node/${node}`}>{node}</Link> /
      </div>
      <div className="row">
        <h1 style={{ margin: 0 }} className="mono">
          {name}
        </h1>
        {rs.sealed && <Badge kind="sealed">sealed</Badge>}
        <span>{rs.purpose}</span>
        <span className="spacer" />
        <span className="muted">{fmtTime(rs.created)}</span>
      </div>
      {rs.derived_from && (
        <p className="muted">
          derived from <Link to={`/runset/${rs.derived_from}`}>{rs.derived_from}</Link>
        </p>
      )}
      <div className="row" style={{ margin: "10px 0" }}>
        {tasks.map((t, i) => (
          <span key={t.id} className="row" style={{ gap: 4 }}>
            {i > 0 && <span className="muted">→</span>}
            <button className={`chip ${t.id === pick?.id ? "active" : ""}`} onClick={() => setSp({ task: t.id }, { replace: true })}>
              <b>{t.kind}</b> <StateBadge state={t.state} />
              {t.state === "running" && t.progress_pct != null && <span className="small">{t.progress_pct.toFixed(0)}%</span>}
              {ACTIVE.includes(t.state) && t.eta_s != null && (
                <span className="small muted">
                  <Eta seconds={t.eta_s} />
                </span>
              )}
              {t.blocked && <Badge kind="blocked">blocked</Badge>}
            </button>
          </span>
        ))}
      </div>
      {pick && (
        <div className="card" style={{ marginBottom: 10 }}>
          <div className="row">
            <b>{pick.kind}</b> <span className="mono small">{pick.id}</span> <span>{countsText(pick.counts)}</span> <span className="muted">{pick.note}</span>
          </div>
          {pick.failure != null && <pre className="error mono small">{typeof pick.failure === "string" ? pick.failure : JSON.stringify(pick.failure, null, 2)}</pre>}
          <TaskActions task={pick} rsRef={ref} />
        </div>
      )}
      <Tabs tabs={TABS} value={tab} onChange={(t) => setParam("tab", t)} />
      {tab === "jobs" && pick && (
        <div>
          <JobsTable task={pick} job={job} onJob={(j) => setParam("job", j)} />
          <h3>
            Log {job ? <span className="mono">{job}</span> : `(${pick.kind})`}
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
      {tab === "post" && <PostTab rs={rs} />}
      {tab === "files" && <FileBrowser dir={rs.dir} depth={3} />}
      {tab === "problem log" && <ProblemLogTab rs={rs} task={pick} />}
      {tab === "provenance" && (
        <div>
          <h3>binary</h3>
          <Json value={rs.bin ?? null} />
          <h3>provenance</h3>
          <Json value={rs.provenance ?? {}} />
          <p className="muted mono small">{rs.dir}</p>
        </div>
      )}
    </div>
  );
}
