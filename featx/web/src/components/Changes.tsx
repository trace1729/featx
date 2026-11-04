import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { useI18n } from "../i18n";
import { Badge, ErrorBox, Loading } from "./ui";
import { foldUntracked, shortCommit, untrackedDirCount, type UntrackedNode } from "../util";
import type { Changes, CodeRepo } from "../types";

export type DiffFetcher = (sub: string, path: string) => Promise<{ diff: string; truncated: boolean }>;

export function DiffView({ text }: { text: string }) {
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

function FileDiff({ scope, sub, file, fetchDiff }: { scope: string; sub: string; file: string; fetchDiff: DiffFetcher }) {
  const { t } = useI18n();
  const q = useQuery({
    queryKey: ["diff", scope, sub, file],
    queryFn: () => fetchDiff(sub, file),
  });
  if (q.isLoading) return <Loading what="diff" />;
  if (q.error) return <ErrorBox error={q.error} />;
  return (
    <div>
      {q.data!.truncated && <div className="notice">{t("diff truncated")}</div>}
      {q.data!.diff ? <DiffView text={q.data!.diff} /> : <div className="muted small">{t("no textual diff (binary or mode change)")}</div>}
    </div>
  );
}

function UntrackedTree({ nodes, sub, level, scope, fetchDiff }: { nodes: UntrackedNode[]; sub: string; level: number; scope: string; fetchDiff: DiffFetcher }) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState<string | null>(null);
  const toggle = (path: string) =>
    setExpanded((old) => {
      const next = new Set(old);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  return (
    <>
      {nodes.map((node) => {
        if (node.type === "dir") {
          const isOpen = expanded.has(node.path);
          return (
            <Fragment key={`d:${node.path}`}>
              <tr className="clickable" onClick={() => toggle(node.path)}>
                <td className="mono">{isOpen ? "▾" : "▸"}</td>
                <td />
                <td />
                <td className="mono" style={{ paddingLeft: level * 14 }}>
                  {node.name}/ <span className="muted">({node.count})</span>
                </td>
              </tr>
              {isOpen && <UntrackedTree nodes={node.children} sub={sub} level={level + 1} scope={scope} fetchDiff={fetchDiff} />}
            </Fragment>
          );
        }
        return (
          <Fragment key={`f:${node.path}`}>
            <tr className={`clickable ${open === node.path ? "selected" : ""}`} onClick={() => setOpen(open === node.path ? null : node.path)}>
              <td className="mono">?</td>
              <td />
              <td />
              <td className="mono" style={{ paddingLeft: 14 + level * 14 }}>
                {node.name} <Badge>{t("untracked")}</Badge>
              </td>
            </tr>
            {open === node.path && (
              <tr>
                <td colSpan={4}>
                  <FileDiff scope={scope} sub={sub} file={node.path} fetchDiff={fetchDiff} />
                </td>
              </tr>
            )}
          </Fragment>
        );
      })}
    </>
  );
}

function UntrackedSection({ paths, sub, total, truncated, scope, fetchDiff }: { paths: string[]; sub: string; total: number; truncated: boolean; scope: string; fetchDiff: DiffFetcher }) {
  const { t } = useI18n();
  const [show, setShow] = useState(false);
  const nodes = foldUntracked(paths);
  const dirs = untrackedDirCount(paths);
  return (
    <div style={{ marginTop: 8 }}>
      <button className="link small" onClick={() => setShow(!show)}>
        {show ? "▾" : "▸"} {t("untracked: {count} files", { count: total })}
        {dirs > 0 ? t(" in {count} dirs", { count: dirs }) : ""}
        {truncated ? t(" (list truncated)") : ""}
      </button>
      {show && (
        <table className="small" style={{ marginTop: 4 }} data-testid="untracked-tree">
          <tbody>
            <UntrackedTree nodes={nodes} sub={sub} level={0} scope={scope} fetchDiff={fetchDiff} />
          </tbody>
        </table>
      )}
    </div>
  );
}

/** A repo with nothing to show: no changed or untracked files and no commits of its own. */
function unchanged(r: CodeRepo): boolean {
  return !!r.exists && !r.error && !r.patch_error && !(r.files ?? []).length && !(r.untracked_total ?? (r.untracked ?? []).length) && !r.ahead && !r.truncated;
}

export function RepoChangesView({ r, sub, scope, fetchDiff, rootLabel = "worktree", vs }: {
  r: CodeRepo; sub: string; scope: string; fetchDiff: DiffFetcher; rootLabel?: string; vs?: string;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState<string | null>(null);
  const title = sub || rootLabel;
  if (!r.exists) return <div className="notice">{title}: {t("{path} is missing on disk", { path: r.path })}</div>;
  if (r.error) return <div className="notice">{title}: {r.error}</div>;
  const files = r.files ?? [];
  const untracked = r.untracked ?? [];
  const total = r.untracked_total ?? untracked.length;
  const toggle = (f: string) => setOpen(open === f ? null : f);
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>
        {title}{" "}
        <span className="muted small mono" title={vs ? undefined : r.base}>
          {vs ? `${t("vs")} ${vs}` : r.base ? `${t("vs")} ${shortCommit(r.base)}` : ""}
        </span>
      </h3>
      <div className="muted small">
        {t("{ahead} commit(s) · {files} file(s) changed, +{added} −{deleted}", { ahead: r.ahead ?? 0, files: files.length, added: r.added ?? 0, deleted: r.deleted ?? 0 })}
        {total > 0 && t(" · {count} untracked", { count: total })}
      </div>
      {r.note && <div className="muted small">{r.note}</div>}
      {r.patch_error && <div className="notice small">{r.patch_error}</div>}
      {!files.length && !total && !r.ahead && <div className="muted">{t("no changes")}</div>}
      {(r.commits ?? []).length > 0 && (
        <table className="small" style={{ marginTop: 8 }}>
          <tbody>
            {r.commits!.map((c) => (
              <tr key={c.sha}>
                <td className="mono" title={c.sha}>{shortCommit(c.sha)}</td>
                <td>{c.subject}</td>
                <td className="muted">{c.author}</td>
                <td className="muted">{c.when}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {files.length > 0 && (
        <table className="small" style={{ marginTop: 8 }}>
          <thead>
            <tr>
              <th>st</th>
              <th className="num">+</th>
              <th className="num">−</th>
              <th>{t("file")}</th>
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
                  {f.path} {f.uncommitted && <Badge kind="warn">{t("uncommitted")}</Badge>}
                </td>
              </tr>,
              open === f.path && (
                <tr key={`${f.path}:diff`}>
                  <td colSpan={4}>
                    <FileDiff scope={scope} sub={sub} file={f.path} fetchDiff={fetchDiff} />
                  </td>
                </tr>
              ),
            ])}
          </tbody>
        </table>
      )}
      {total > 0 && (
        <UntrackedSection paths={untracked} sub={sub} total={total} truncated={!!r.untracked_truncated} scope={scope} fetchDiff={fetchDiff} />
      )}
      {(r.omitted ?? []).length > 0 && (
        <div className="muted small" title={r.omitted!.map((o) => `${o.path}: ${o.reason}`).join("\n")}>
          {t("untracked, not saved")}: <span className="mono">{r.omitted!.slice(0, 3).map((o) => o.path).join(", ")}{r.omitted!.length > 3 ? `, … (${r.omitted!.length})` : ""}</span>
        </div>
      )}
      {r.truncated && <div className="muted small">{t("file list truncated")}</div>}
    </div>
  );
}

export function ChangesView({ changes, scope, fetchDiff, rootLabel = "worktree", vs }: {
  changes?: Changes; scope: string; fetchDiff: DiffFetcher; rootLabel?: string; vs?: string;
}) {
  const { t } = useI18n();
  const repos = Object.entries(changes?.repos ?? {});
  if (!repos.length) return <div className="muted">{t("no changes")}</div>;
  // unchanged repos (a node usually carries a dozen submodules) are listed on one line
  const same = repos.filter(([, r]) => unchanged(r));
  const shown = repos.filter(([, r]) => !unchanged(r));
  return (
    <div>
      {!shown.length && <div className="muted" data-testid="no-differences">{t("no differences")}</div>}
      {shown.map(([sub, r]) => (
        <RepoChangesView key={sub || "."} sub={sub} r={r} scope={scope} fetchDiff={fetchDiff} rootLabel={rootLabel} vs={vs} />
      ))}
      {same.length > 0 && (
        <div className="muted small" data-testid="unchanged-repos">
          {t("unchanged")} ({same.length}): <span className="mono">{same.map(([sub]) => sub || rootLabel).join(", ")}</span>
        </div>
      )}
    </div>
  );
}

