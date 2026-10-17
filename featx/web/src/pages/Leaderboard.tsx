import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Badge, ErrorBox, Loading, VerdictMark } from "../components/ui";
import type { Board } from "../types";
import { fmtTime, fmtValue, loadStored, metricLabel, saveStored } from "../util";
import { useI18n } from "../i18n";

// the board's choices (metric, class, order, checkboxes) live in the URL; the last ones are
// remembered and restored when the page is opened without any
const REMEMBERED = ["metric", "cls", "order", "best", "all"];

export function LeaderboardPage() {
  const { t } = useI18n();
  const [sp, setSp] = useSearchParams();
  const nav = useNavigate();
  const set = (key: string, value: string | null) => {
    const next = new URLSearchParams(sp);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key === "metric") next.delete("cls"); // classes belong to the metric's post step
    setSp(next, { replace: true });
  };
  useEffect(() => {
    if ([...sp.keys()].length) return;
    const last = loadStored<Record<string, string>>("leaderboard", {});
    const keep = Object.entries(last).filter(([k, v]) => REMEMBERED.includes(k) && v);
    if (keep.length) setSp(new URLSearchParams(keep), { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    const now = Object.fromEntries(REMEMBERED.filter((k) => sp.get(k)).map((k) => [k, sp.get(k)!]));
    if (Object.keys(now).length) saveStored("leaderboard", now);
  }, [sp]);
  const params = {
    metric: sp.get("metric"),
    cls: sp.get("cls"),
    node: sp.get("node"),
    order: sp.get("order"),
    best_per_node: sp.get("best") === "1",
    include_incomplete: sp.get("all") === "1",
  };
  const q = useQuery({
    queryKey: ["leaderboard", params],
    queryFn: () => api.get<Board>("/leaderboard", params),
    refetchInterval: 15000,
    placeholderData: keepPreviousData,
  });
  if (q.isLoading) return <Loading what={t("Leaderboard")} />;
  if (q.error) return <ErrorBox error={q.error} />;
  const b = q.data!;
  if (!b.metric) {
    return (
      <div>
        <h1>{t("Leaderboard")}</h1>
        <p className="muted">No runset has a post-processing summary yet. Submit a node with a post recipe (for example the perf-keys plugin).</p>
      </div>
    );
  }
  const cls = b.classes.find((c) => c.key === b.class);
  const max = Math.max(...b.rows.map((r) => Math.abs(r.value)), 1e-12);
  return (
    <div>
      <h1>{t("Leaderboard")}</h1>
      <div className="card row">
        <label>{t("metric")}</label>
        <select value={b.metric} onChange={(e) => set("metric", e.target.value)}>
          {b.metrics.map((m) => (
            <option key={m.name} value={m.name}>
              {metricLabel(m.name)} · {m.runsets} runsets
            </option>
          ))}
        </select>
        <label>{t("class")}</label>
        <select value={b.class ?? ""} onChange={(e) => set("cls", e.target.value)}>
          {b.classes.map((c) => (
            <option key={c.key} value={c.key}>
              {c.label} · {c.complete_rows}/{c.rows} complete
            </option>
          ))}
        </select>
        <button onClick={() => set("order", b.order === "asc" ? "desc" : "asc")} title="sort order">
          {t(b.order === "asc" ? "lowest first ▴" : "highest first ▾")}
        </button>
        <label>
          <input type="checkbox" checked={params.best_per_node} onChange={(e) => set("best", e.target.checked ? "1" : null)} /> {t("best per node")}
        </label>
        <label>
          <input type="checkbox" checked={params.include_incomplete} onChange={(e) => set("all", e.target.checked ? "1" : null)} /> {t("show incomplete")}
        </label>
        {params.node && (
          <span className="chip">
            under <span className="mono">{params.node}</span>
            <button className="link" onClick={() => set("node", null)}>
              ×
            </button>
          </span>
        )}
      </div>
      <p className="muted small">
        Runsets are only ranked against others with the same reference, PERF block, window and point set
        {cls ? ` (${cls.label})` : ""}. {b.classes.length > 1 ? `${b.classes.length - 1} other class(es) are in the selector.` : ""}
      </p>
      {b.rows.length === 0 ? (
        <div className="notice">No complete runset in this class. Tick “show incomplete” to list the partial ones.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th className="num">#</th>
              <th className="num">{metricLabel(b.metric)}</th>
              <th style={{ width: "18%" }} />
              <th>runset</th>
              <th>points</th>
              <th>purpose</th>
              <th>binary</th>
              <th>created</th>
            </tr>
          </thead>
          <tbody>
            {b.rows.map((r) => (
              <tr key={r.runset} className={`clickable ${r.complete ? "" : "dim"}`} onClick={() => nav(`/runset/${r.runset}`)}>
                <td className="num">{r.rank ?? "–"}</td>
                <td className="num mono">{fmtValue(r.value)}</td>
                <td>
                  <div className="bar" style={{ width: `${(100 * Math.abs(r.value)) / max}%` }} />
                </td>
                <td>
                  <VerdictMark verdict={r.verdict} />{" "}
                  <Link to={`/node/${r.node}`} onClick={(e) => e.stopPropagation()} className="mono">
                    {r.node}
                  </Link>
                  <span className="mono muted">/{r.name}</span> {r.sealed && <Badge kind="sealed">sealed</Badge>}
                </td>
                <td>
                  {r.points !== null ? `${r.common_points}/${r.points}` : ""} {!r.complete && <Badge kind="warn">incomplete</Badge>}
                </td>
                <td>{r.purpose}</td>
                <td className="mono small">{r.bin}</td>
                <td className="muted">{fmtTime(r.created)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
