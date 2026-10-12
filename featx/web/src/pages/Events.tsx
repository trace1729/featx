import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, ErrorBox, Loading, useAction } from "../components/ui";
import type { EventRow } from "../types";
import { fmtTime } from "../util";

const TYPES = ["failure", "retry", "budget_exhausted", "task_done", "runset_done", "submitted", "relaunched", "cancelled"];

function detail(e: EventRow): string {
  if (e.type === "failure" || e.type === "retry") {
    const f = (e.failure ?? {}) as { signature?: string; class?: string };
    return [e.job, e.signature ?? f.signature, f.class].filter(Boolean).join(" · ");
  }
  if (e.type === "task_done") return `${e.kind} ${e.state}`;
  if (e.type === "runset_done") return "all tasks succeeded";
  if (e.type === "budget_exhausted") return `design relaunch budget used up (${e.relaunches}); needs featx task unblock ${e.task}`;
  if (e.type === "relaunched") return [Array.isArray(e.jobs) ? e.jobs.join(", ") : "", e.reason].filter(Boolean).join(" · ");
  return "";
}

export function EventsPage() {
  const [showAcked, setShowAcked] = useState(false);
  const [types, setTypes] = useState<string[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const qc = useQueryClient();
  const act = useAction();
  const q = useQuery({
    queryKey: ["events", showAcked, types],
    queryFn: () => api.get<EventRow[]>("/events", { unacked: !showAcked, types: types.join(","), limit: 500 }),
    refetchInterval: 5000,
  });
  const ack = (body: { seqs?: number[]; upto?: number }) =>
    act.run(async () => {
      await api.post("/events/ack", body);
      await Promise.all([qc.invalidateQueries({ queryKey: ["events"] }), qc.invalidateQueries({ queryKey: ["status"] })]);
    });
  const rows = [...(q.data ?? [])].sort((a, b) => b.seq - a.seq);
  return (
    <div>
      <h1>Events</h1>
      <div className="card row">
        {TYPES.map((t) => (
          <label key={t}>
            <input type="checkbox" checked={types.includes(t)} onChange={(e) => setTypes(e.target.checked ? [...types, t] : types.filter((x) => x !== t))} /> {t}
          </label>
        ))}
        <label>
          <input type="checkbox" checked={showAcked} onChange={(e) => setShowAcked(e.target.checked)} /> include acknowledged
        </label>
        <span style={{ flex: 1 }} />
        <button disabled={!rows.some((r) => !r.acked) || act.busy} onClick={() => ack({ seqs: rows.filter((r) => !r.acked).map((r) => r.seq) })}>
          Acknowledge all shown
        </button>
      </div>
      <ErrorBox error={q.error ?? act.error} />
      {q.isLoading ? (
        <Loading what="events" />
      ) : rows.length === 0 ? (
        <div className="muted">no events</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th className="num">seq</th>
              <th>time</th>
              <th>type</th>
              <th>runset</th>
              <th>detail</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <Fragment key={e.seq}>
                <tr className={`clickable ${e.acked ? "dim" : ""}`} onClick={() => setOpen(open === e.seq ? null : e.seq)}>
                  <td className="num">{e.seq}</td>
                  <td className="muted">{fmtTime(e.time)}</td>
                  <td>
                    <Badge kind={e.type === "task_done" || e.type === "runset_done" ? (e.state === "failed" ? "failed" : e.state === "cancelled" ? "cancelled" : "succeeded") : e.type === "budget_exhausted" ? "failed" : e.type}>{e.type}</Badge>
                  </td>
                  <td className="mono">
                    {e.node && e.runset ? (
                      <Link to={`/runset/${e.node}/${e.runset}${e.task ? `?task=${e.task}${e.job ? `&job=${e.job}` : ""}` : ""}`} onClick={(ev) => ev.stopPropagation()}>
                        {e.node}/{e.runset}
                      </Link>
                    ) : (
                      e.node ?? ""
                    )}
                  </td>
                  <td>{detail(e)}</td>
                  <td>
                    {!e.acked && (
                      <button
                        onClick={(ev) => {
                          ev.stopPropagation();
                          void ack({ seqs: [e.seq] });
                        }}
                      >
                        ack
                      </button>
                    )}
                  </td>
                </tr>
                {open === e.seq && (
                  <tr>
                    <td colSpan={6}>
                      <pre className="mono">{JSON.stringify(e, null, 2)}</pre>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
