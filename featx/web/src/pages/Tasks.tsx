import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorBox, Eta, Loading, Progress, StateBadge, useAction } from "../components/ui";
import type { HostRow, Status, TaskView } from "../types";
import { countsText, fmtTime, usePersistentState } from "../util";

const ACTIVE = ["queued", "running", "launching"];

/** Priority of a queued/running task's whole runset: ▲/▼ steps it, the box sets it. */
export function PriorityControl({ task }: { task: TaskView }) {
  const qc = useQueryClient();
  const act = useAction();
  const now = task.priority ?? 0;
  const [text, setText] = useState(String(now));
  useEffect(() => setText(String(now)), [now]);
  const set = (p: number) =>
    act.run(async () => {
      await api.post(`/tasks/${task.id}/priority`, { priority: p, runset: true });
      await qc.invalidateQueries({ queryKey: ["tasks"] });
    });
  if (!ACTIVE.includes(task.state)) return <span className="muted">{now || ""}</span>;
  return (
    <span className="row" style={{ gap: 3, flexWrap: "nowrap" }} title={act.error ? String(act.error) : "launch priority of this runset (higher first)"}>
      <button className="link" aria-label={`raise priority of ${task.id}`} disabled={act.busy} onClick={() => set(now + 1)}>
        ▲
      </button>
      <input
        type="text"
        className="mono"
        style={{ width: "3.5em" }}
        aria-label={`priority of ${task.id}`}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && /^-?\d+$/.test(text.trim())) set(Number(text.trim()));
        }}
        onBlur={() => /^-?\d+$/.test(text.trim()) && Number(text.trim()) !== now && set(Number(text.trim()))}
      />
      <button className="link" aria-label={`lower priority of ${task.id}`} disabled={act.busy} onClick={() => set(now - 1)}>
        ▼
      </button>
      {act.error ? <span className="badge failed">!</span> : null}
    </span>
  );
}

function CapField({ label, used, cap, keyName, onSaved }: { label: string; used: number; cap: number; keyName: string; onSaved: () => void }) {
  const act = useAction();
  const [text, setText] = useState(String(cap));
  useEffect(() => setText(String(cap)), [cap]);
  const valid = /^\d+$/.test(text.trim());
  const save = () =>
    act.run(async () => {
      await api.post("/config/set", { key: keyName, value: Number(text.trim()) });
      onSaved();
    });
  return (
    <div className="row">
      <span style={{ width: "7em" }}>{label}</span>
      <span className="mono">
        {used} / {cap ? cap : "∞"}
      </span>
      <div style={{ width: 120 }}>
        <Progress pct={cap ? (100 * used) / cap : null} />
      </div>
      <input type="text" className="mono cap-input" aria-label={`${keyName}`} value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && valid && save()} />
      <button disabled={!valid || Number(text) === cap || act.busy} onClick={save}>
        Set
      </button>
      <span className="muted small mono">{keyName} (0 = no cap)</span>
      <ErrorBox error={act.error} />
    </div>
  );
}

export function CapacityPanel() {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["status"], queryFn: () => api.get<Status>("/status"), refetchInterval: 5000 });
  const s = status.data;
  if (!s || s.max_total_cores === undefined) return null;
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["status"] });
    qc.invalidateQueries({ queryKey: ["config"] });
  };
  return (
    <div className="card">
      <CapField label="run cores" used={s.run_cores_in_use ?? 0} cap={s.max_total_cores ?? 0} keyName="placement.max_total_cores" onSaved={refresh} />
      <CapField label="build cores" used={s.build_cores_in_use ?? 0} cap={s.max_build_cores ?? 0} keyName="placement.max_build_cores" onSaved={refresh} />
      <div className="muted small">
        Saved in the featx.yaml user layer and applied on the next scheduler tick; queued tasks wait for cores in priority order, running jobs are never stopped.
      </div>
    </div>
  );
}

export function TaskTable({ tasks }: { tasks: TaskView[] }) {
  if (!tasks.length) return <div className="muted">none</div>;
  return (
    <table>
      <thead>
        <tr>
          <th>runset</th>
          <th>stage</th>
          <th>state</th>
          <th title="launch priority (higher first)">prio</th>
          <th>jobs</th>
          <th>progress</th>
          <th>ETA</th>
          <th>created</th>
        </tr>
      </thead>
      <tbody>
        {tasks.map((t) => (
          <tr key={t.id}>
            <td className="mono">
              <Link to={`/runset/${t.node}/${t.runset}?task=${t.id}`}>
                {t.node}/{t.runset}
              </Link>
            </td>
            <td>{t.kind}</td>
            <td>
              <StateBadge state={t.state} /> {t.blocked && <span className="badge blocked">blocked</span>} <span className="muted small">{t.note}</span>
            </td>
            <td>
              <PriorityControl task={t} />
            </td>
            <td>{countsText(t.counts)}</td>
            <td>{t.state === "running" ? <Progress pct={t.progress_pct ?? null} /> : ""}</td>
            <td>{ACTIVE.includes(t.state) ? <Eta seconds={t.eta_s} /> : ""}</td>
            <td className="muted">{fmtTime(t.created)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function TasksPage() {
  const q = useQuery({ queryKey: ["tasks", "all"], queryFn: () => api.get<TaskView[]>("/tasks", { limit: 200 }), refetchInterval: 5000 });
  const hosts = useQuery({ queryKey: ["hosts"], queryFn: () => api.get<HostRow[]>("/hosts"), refetchInterval: 60000 });
  const [showDone, setShowDone] = usePersistentState("tasks:showFinished", true);
  const [filter, setFilter] = usePersistentState("tasks:filter", "");
  if (q.isLoading) return <Loading what="tasks" />;
  const all = q.data ?? [];
  const f = filter.trim();
  const mine = (t: TaskView) => !f || t.node.includes(f) || t.runset.includes(f) || t.id.includes(f);
  const done = all.filter((t) => !ACTIVE.includes(t.state) && mine(t)).sort((a, b) => b.created.localeCompare(a.created));
  const active = all.filter((t) => ACTIVE.includes(t.state) && mine(t));
  // launch order: priority first, FIFO within a priority (as the scheduler picks)
  const order = (a: TaskView, b: TaskView) => (b.priority ?? 0) - (a.priority ?? 0) || a.created.localeCompare(b.created);
  active.sort(order);
  return (
    <div>
      <h1>Tasks</h1>
      <ErrorBox error={q.error} />
      <h2>Capacity</h2>
      <CapacityPanel />
      <div className="row">
        <input type="text" placeholder="filter by node / runset / task id" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <label>
          <input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} /> show finished
        </label>
      </div>
      <h2>Active ({active.length})</h2>
      <TaskTable tasks={active} />
      {showDone && (
        <>
          <h2>Finished ({done.length})</h2>
          <TaskTable tasks={done.slice(0, 100)} />
        </>
      )}
      <h2>Hosts</h2>
      <table>
        <thead>
          <tr>
            <th>host</th>
            <th className="num">free cores</th>
            <th className="num">free mem GB</th>
            <th className="num">load</th>
            <th className="num">featx jobs</th>
            <th>state</th>
          </tr>
        </thead>
        <tbody>
          {(hosts.data ?? []).map((h) => (
            <tr key={h.host} className={h.free_cores === undefined ? "dim" : ""}>
              <td className="mono">{h.host}</td>
              <td className="num">{h.free_cores?.toFixed(1) ?? ""}</td>
              <td className="num">{h.free_mem_gb?.toFixed(0) ?? ""}</td>
              <td className="num">{h.load1 !== undefined ? `${h.load1.toFixed(0)} / ${h.cores}` : ""}</td>
              <td className="num">{h.featx_jobs ?? 0}</td>
              <td>{h.bad ? <span className="badge failed">avoided</span> : h.error ? <span className="badge failed">{h.error}</span> : h.state ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
