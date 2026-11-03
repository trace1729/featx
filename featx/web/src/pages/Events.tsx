import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, ErrorBox, Loading, useAction } from "../components/ui";
import type { EventRow } from "../types";
import { fmtTime } from "../util";
import { useI18n } from "../i18n";

const TYPES = ["failure", "retry", "budget_exhausted", "task_done", "runset_done", "review_needed", "submitted", "relaunched", "cancelled"];

function detail(e: EventRow, t: (text: string, values?: Record<string, string | number>) => string): string {
  if (e.type === "failure" || e.type === "retry") {
    const f = (e.failure ?? {}) as { signature?: string; class?: string };
    return [e.job, e.signature ?? f.signature, f.class].filter(Boolean).join(" · ");
  }
  if (e.type === "task_done") return `${t(String(e.kind))} ${t(String(e.state))}`;
  if (e.type === "runset_done") return t("all tasks succeeded");
  if (e.type === "review_needed") return t("Review the verdict and evidence for this completed runset");
  if (e.type === "budget_exhausted") return t("design relaunch budget used up ({count}); needs featx task unblock {task}", { count: Number(e.relaunches ?? 0), task: e.task ?? "" });
  if (e.type === "relaunched") return [Array.isArray(e.jobs) ? e.jobs.join(", ") : "", e.reason].filter(Boolean).join(" · ");
  return "";
}

export function EventsPage() {
  const { t } = useI18n();
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
      <h1>{t("Events")}</h1>
      <div className="card row">
        {TYPES.map((type) => (
          <label key={type}>
            <input type="checkbox" checked={types.includes(type)} onChange={(e) => setTypes(e.target.checked ? [...types, type] : types.filter((x) => x !== type))} /> {t(type)}
          </label>
        ))}
        <label>
          <input type="checkbox" checked={showAcked} onChange={(e) => setShowAcked(e.target.checked)} /> {t("include acknowledged")}
        </label>
        <span style={{ flex: 1 }} />
        <button disabled={!rows.some((r) => !r.acked) || act.busy} onClick={() => ack({ seqs: rows.filter((r) => !r.acked).map((r) => r.seq) })}>
          {t("Acknowledge all shown")}
        </button>
      </div>
      <ErrorBox error={q.error ?? act.error} />
      {q.isLoading ? (
        <Loading what="events" />
      ) : rows.length === 0 ? (
        <div className="muted">{t("no events")}</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th className="num">seq</th>
              <th>{t("time")}</th>
              <th>{t("type")}</th>
              <th>{t("runset")}</th>
              <th>{t("detail")}</th>
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
                    <Badge kind={e.type === "task_done" || e.type === "runset_done" ? (e.state === "failed" ? "failed" : e.state === "cancelled" ? "cancelled" : "succeeded") : e.type === "budget_exhausted" ? "failed" : e.type}>{t(e.type)}</Badge>
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
                  <td>{detail(e, t)}</td>
                  <td>
                    {!e.acked && (
                      <button
                        onClick={(ev) => {
                          ev.stopPropagation();
                          void ack({ seqs: [e.seq] });
                        }}
                      >
                        {t("ack")}
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
