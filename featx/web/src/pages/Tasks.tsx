import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorBox, Eta, Loading, Progress, StateBadge } from "../components/ui";
import type { HostRow, TaskView } from "../types";
import { countsText, fmtTime } from "../util";

const ACTIVE = ["queued", "running", "launching"];

export function TaskTable({ tasks }: { tasks: TaskView[] }) {
  if (!tasks.length) return <div className="muted">none</div>;
  return (
    <table>
      <thead>
        <tr>
          <th>runset</th>
          <th>stage</th>
          <th>state</th>
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
  if (q.isLoading) return <Loading what="tasks" />;
  const all = q.data ?? [];
  const active = all.filter((t) => ACTIVE.includes(t.state));
  const done = all.filter((t) => !ACTIVE.includes(t.state)).sort((a, b) => b.created.localeCompare(a.created));
  return (
    <div>
      <h1>Tasks</h1>
      <ErrorBox error={q.error} />
      <h2>Active ({active.length})</h2>
      <TaskTable tasks={active} />
      <h2>Finished ({done.length})</h2>
      <TaskTable tasks={done.slice(0, 100)} />
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
