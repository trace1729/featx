import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Dialog, ErrorBox, Loading, useAction } from "../components/ui";
import type { GcReport, StorageReport } from "../types";
import { fmtBytes } from "../util";
import { useI18n } from "../i18n";

export function StoragePage() {
  const { t } = useI18n();
  const [worktrees, setWorktrees] = useState(false);
  const [buildDirs, setBuildDirs] = useState(false);
  const [gc, setGc] = useState<GcReport | null>(null);
  const [confirm, setConfirm] = useState(false);
  const qc = useQueryClient();
  const act = useAction();
  const q = useQuery({ queryKey: ["storage", worktrees], queryFn: () => api.get<StorageReport>("/storage", { worktrees }), staleTime: 30000 });
  const runGc = (apply: boolean) =>
    act.run(async () => {
      const r = await api.post<GcReport>("/gc", { apply, build_dirs: buildDirs });
      setGc(r);
      setConfirm(false);
      if (apply) await qc.invalidateQueries({ queryKey: ["storage"] });
    });
  if (q.isLoading) return <Loading what={t("Storage")} />;
  if (q.error) return <ErrorBox error={q.error} />;
  const r = q.data!;
  const max = Math.max(...r.nodes.map((n) => n.bytes), ...r.bincache.map((b) => b.bytes), 1);
  return (
    <div>
      <h1>{t("Storage")}</h1>
      <p className="muted mono">
        {r.root} · {r.total_human}
      </p>
      <label>
        <input type="checkbox" checked={worktrees} onChange={(e) => setWorktrees(e.target.checked)} /> also size the worktrees (slow)
      </label>
      <h2>{t("Nodes")}</h2>
      <table>
        <thead>
          <tr>
            <th>node</th>
            <th className="num">runsets</th>
            <th className="num">size</th>
            <th style={{ width: "35%" }} />
            {worktrees && <th className="num">worktree</th>}
          </tr>
        </thead>
        <tbody>
          {r.nodes.map((n) => (
            <tr key={n.path}>
              <td className="mono">
                <Link to={`/node/${n.path}`}>{n.path}</Link>
              </td>
              <td className="num">{n.runsets}</td>
              <td className="num">{n.human}</td>
              <td>
                <div className="bar" style={{ width: `${(100 * n.bytes) / max}%` }} />
              </td>
              {worktrees && <td className="num">{n.worktree_human ?? ""}</td>}
            </tr>
          ))}
        </tbody>
      </table>
      <h2>{t("Binary cache")}</h2>
      {r.bincache.length === 0 ? (
        <div className="muted">empty</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>key</th>
              <th>label</th>
              <th className="num">size</th>
              <th>referenced by</th>
            </tr>
          </thead>
          <tbody>
            {r.bincache.map((b) => (
              <tr key={b.key}>
                <td className="mono">{b.key}</td>
                <td>{b.label}</td>
                <td className="num">{b.human}</td>
                <td className="mono small">{b.refs.length ? b.refs.join(", ") : <span className="badge warn">unreferenced</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h2>{t("Trash")}</h2>
      {r.trash.length === 0 ? (
        <div className="muted">empty</div>
      ) : (
        <table>
          <tbody>
            {r.trash.map((t) => (
              <tr key={t.path}>
                <td className="mono">{t.path}</td>
                <td className="num">{t.human}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="muted">compare/collect-cache: {fmtBytes(r.collect_cache_bytes)}</p>

      <h2>{t("Garbage collection")}</h2>
      <div className="row">
        <label>
          <input type="checkbox" checked={buildDirs} onChange={(e) => setBuildDirs(e.target.checked)} /> include worktree build/ dirs of done or abandoned nodes
        </label>
        <button disabled={act.busy} onClick={() => runGc(false)}>
          {t("Dry run")}
        </button>
      </div>
      <ErrorBox error={act.error} />
      {gc && (
        <div className="card" style={{ marginTop: 10 }}>
          {gc.applied ? <div className="okbox">Deleted {gc.candidates.length} item(s), {gc.total_human}.</div> : <div>{gc.candidates.length} candidate(s), {gc.total_human}. Nothing referenced by a runset, baseline or active task is listed.</div>}
          {gc.candidates.length > 0 && (
            <table style={{ marginTop: 8 }}>
              <tbody>
                {gc.candidates.map((c) => (
                  <tr key={c.path}>
                    <td>{c.kind}</td>
                    <td className="mono small">{c.path}</td>
                    <td className="num">{c.human}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {!gc.applied && gc.candidates.length > 0 && (
            <div className="dialog-actions">
              <button className="danger" onClick={() => setConfirm(true)}>
                {t("Delete these…")}
              </button>
            </div>
          )}
        </div>
      )}
      <Dialog open={confirm} onClose={() => setConfirm(false)} title={t("Delete permanently?")}>
        <p>
          {gc?.candidates.length} item(s), {gc?.total_human}. This cannot be undone.
        </p>
        <div className="dialog-actions">
          <button onClick={() => setConfirm(false)}>{t("Cancel")}</button>
          <button className="danger" disabled={act.busy} onClick={() => runGc(true)}>
            {t("Delete")}
          </button>
        </div>
      </Dialog>
    </div>
  );
}
