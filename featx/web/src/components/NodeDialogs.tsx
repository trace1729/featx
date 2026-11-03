import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api";
import type { MovePlan } from "../types";
import { Dialog, ErrorBox, useAction } from "./ui";
import { useI18n } from "../i18n";

/** Refresh everything that shows the tree. Queries of a node that no longer exists at `gone`
 * (moved or removed) are dropped instead of refetched: refetching them only yields a 404-style error. */
function useRefreshTree() {
  const qc = useQueryClient();
  return (gone?: string) => {
    const isGone = (key: readonly unknown[]) =>
      !!gone && key[0] === "node" && typeof key[1] === "string" && (key[1] === gone || key[1].startsWith(`${gone}/`));
    if (gone) qc.removeQueries({ predicate: (q) => isGone(q.queryKey) });
    return Promise.all(
      ["tree", "node", "leaderboard", "storage"].map((k) => qc.invalidateQueries({ queryKey: [k], predicate: (q) => !isGone(q.queryKey) })),
    );
  };
}

export function AddNodeDialog({ parent, onClose, onDone }: { parent: string; onClose: () => void; onDone: (path: string) => void }) {
  const { t } = useI18n();
  const [name, setName] = useState("");
  const [hypothesis, setHypothesis] = useState("");
  const [worktree, setWorktree] = useState(false);
  const act = useAction();
  const refresh = useRefreshTree();
  const path = parent ? `${parent}/${name}` : name;
  const submit = () =>
    act.run(async () => {
      await api.post("/nodes", { path, hypothesis, worktree, parents: true });
      await refresh();
      onDone(path);
    });
  return (
    <Dialog open onClose={onClose} title={parent ? t("Add a child of {parent}", { parent }) : t("Add a category")}>
      <div className="form">
        <label>{t("path")}</label>
        <span className="mono">
          {parent ? `${parent}/` : ""}
          <input type="text" autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder={t("name (a/b creates both)")} style={{ width: 240 }} />
        </span>
        <label>{t("hypothesis")}</label>
        <textarea rows={3} value={hypothesis} onChange={(e) => setHypothesis(e.target.value)} placeholder={t("what this node is meant to show")} />
        {!hypothesis.trim() && <p className="muted small">{t("A hypothesis is recommended; you can add it now or edit it after the experiment starts.")}</p>}
        <label>{t("worktree")}</label>
        <label>
          <input type="checkbox" checked={worktree} onChange={(e) => setWorktree(e.target.checked)} /> {t("create a git worktree and branch now (takes a while)")}
        </label>
      </div>
      <ErrorBox error={act.error} />
      <div className="dialog-actions">
        <button onClick={onClose}>{t("Cancel")}</button>
        <button className="primary" disabled={!name.trim() || act.busy} onClick={submit}>
          {act.busy ? t("Creating…") : t("Create")}
        </button>
      </div>
    </Dialog>
  );
}

export function PlanView({ plan }: { plan: MovePlan }) {
  const { t } = useI18n();
  return (
    <div>
      {plan.create_parents && plan.create_parents.length > 0 && (
        <div className="notice">{t("creates missing parent nodes: {nodes}", { nodes: plan.create_parents.join(", ") })}</div>
      )}
      <table>
        <thead>
          <tr>
            <th>{t("node")}</th>
            <th>{t("becomes")}</th>
            <th>{t("kind")}</th>
            <th className="num">{t("runsets")}</th>
            <th>{t("worktree / branch")}</th>
          </tr>
        </thead>
        <tbody>
          {plan.nodes.map((n) => (
            <tr key={n.from}>
              <td className="mono">{n.from}</td>
              <td className="mono">{n.to}</td>
              <td>{t(n.kind ?? "")}</td>
              <td className="num">{n.runsets ?? ""}</td>
              <td className="mono small">
                {n.git?.worktree && (
                  <div>
                    {t("dir")} {n.git.worktree.from} → {n.git.worktree.to}
                  </div>
                )}
                {n.git?.worktree_kept && <div className="muted">{t("dir stays at")} {n.git.worktree_kept}</div>}
                {n.git?.branch && <div>{t("branch")} {n.git.branch.from} → {n.git.branch.to}
                  {n.git.branch.submodules?.length ? ` (+ ${n.git.branch.submodules.join(", ")})` : ""}</div>}
                {n.git?.error && <div className="error">{n.git.error}</div>}
                {!n.git && <span className="muted">{t("none")}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">{t("Old paths keep resolving through forwarding symlinks. Nothing has been changed yet.")}</p>
    </div>
  );
}

export function MoveDialog({ src, initialDst, onClose, onDone }: { src: string; initialDst?: string; onClose: () => void; onDone: (dst: string) => void }) {
  const { t } = useI18n();
  const [dst, setDst] = useState(initialDst ?? src);
  const [parents, setParents] = useState(true);
  const [rename, setRename] = useState(true);
  const [debounced, setDebounced] = useState(dst);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(dst.trim().replace(/^\/+|\/+$/g, "")), 300);
    return () => clearTimeout(t);
  }, [dst]);
  const body = { src, dst: debounced, parents, rename_branches: rename };
  const same = debounced === src || !debounced;
  // the daemon runs every check of the real move and reports what would change
  const plan = useQuery({
    queryKey: ["mv-plan", body],
    queryFn: () => api.post<MovePlan>("/nodes/mv", { ...body, dry_run: true }),
    enabled: !same,
    retry: false,
    staleTime: 0,
  });
  const act = useAction();
  const refresh = useRefreshTree();
  const blocked = same || !plan.data || plan.isFetching || plan.data.nodes.some((n) => n.git?.error);
  const apply = () =>
    act.run(async () => {
      await api.post("/nodes/mv", body);
      // leave the old path before the refresh, or its page refetches a node that is gone
      onDone(debounced);
      await refresh(src);
    });
  return (
    <Dialog open onClose={onClose} title={t("Move {src}", { src })}>
      <div className="form">
        <label>{t("new path")}</label>
        <input type="text" className="mono" autoFocus value={dst} onChange={(e) => setDst(e.target.value)} />
        <label>{t("options")}</label>
        <span className="row">
          <label>
            <input type="checkbox" checked={parents} onChange={(e) => setParents(e.target.checked)} /> {t("create missing parents")}
          </label>
          <label>
            <input type="checkbox" checked={rename} onChange={(e) => setRename(e.target.checked)} /> {t("rename the branches featx created")}
          </label>
        </span>
      </div>
      <h3>{t("Dry run")}</h3>
      {same ? <div className="muted">{t("enter a different path")}</div> : plan.isFetching ? <div className="muted">{t("checking…")}</div> : plan.error ? <ErrorBox error={plan.error} /> : plan.data ? <PlanView plan={plan.data} /> : null}
      <ErrorBox error={act.error} />
      <div className="dialog-actions">
        <button onClick={onClose}>{t("Cancel")}</button>
        <button className="primary" disabled={blocked || act.busy} onClick={apply}>
          {act.busy ? t("Moving…") : t("Move {count} node(s)", { count: plan.data?.nodes.length ?? 0 })}
        </button>
      </div>
    </Dialog>
  );
}

export function RemoveDialog({ path, onClose, onDone }: { path: string; onClose: () => void; onDone: (result: Record<string, unknown>) => void }) {
  const { t } = useI18n();
  const [typed, setTyped] = useState("");
  const [dropWt, setDropWt] = useState(false);
  const [dropBr, setDropBr] = useState(false);
  const act = useAction();
  const refresh = useRefreshTree();
  const apply = () =>
    act.run(async () => {
      const r = await api.post<Record<string, unknown>>("/nodes/rm", { ref: path, force: true, drop_worktrees: dropWt, drop_branches: dropWt && dropBr });
      onDone(r);
      await refresh(path);
    });
  return (
    <Dialog open onClose={onClose} title={t("Remove {path}", { path })}>
      <p>
        {t("The node, its children and all their runsets move to tree/.trash. They stay there until gc is applied.")}
      </p>
      <p>
        <label>
          <input type="checkbox" checked={dropWt} onChange={(e) => setDropWt(e.target.checked)} /> {t("also delete the worktrees (uncommitted diffs are saved as patches) and the branches featx created that have no own commits")}
        </label>
      </p>
      <p>
        <label>
          <input type="checkbox" disabled={!dropWt} checked={dropWt && dropBr} onChange={(e) => setDropBr(e.target.checked)} /> {t("also delete branches that carry unmerged commits")}
        </label>
      </p>
      <div className="form">
        <label>{t("type the path to confirm")}</label>
        <input type="text" className="mono" value={typed} onChange={(e) => setTyped(e.target.value)} placeholder={path} />
      </div>
      <ErrorBox error={act.error} />
      <div className="dialog-actions">
        <button onClick={onClose}>{t("Cancel")}</button>
        <button className="danger" disabled={typed !== path || act.busy} onClick={apply}>
          {act.busy ? t("Removing…") : t("Remove")}
        </button>
      </div>
    </Dialog>
  );
}
