import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api } from "../api";
import type { TreeRow } from "../types";
import { buildTree, fmtDuration, usePersistentState, type TreeNode } from "../util";
import { AddNodeDialog, MoveDialog } from "./NodeDialogs";
import { Badge, ErrorBox, VerdictMark } from "./ui";
import { useI18n } from "../i18n";

function stage(row: TreeRow): { text: string; state: string } | null {
  const kinds = Object.keys(row.tasks);
  if (!kinds.length) return null;
  // the stage that is running, else the first one that is not done, else the last one
  const pick = kinds.find((k) => ["running", "launching"].includes(row.tasks[k])) ?? kinds.find((k) => row.tasks[k] !== "succeeded") ?? kinds[kinds.length - 1];
  return { text: `${pick}:${row.tasks[pick]}`, state: row.tasks[pick] };
}

function Row({ node, depth, selected, collapsed, toggle, onDropOn }: {
  node: TreeNode;
  depth: number;
  selected: string;
  collapsed: Set<string>;
  toggle: (path: string) => void;
  onDropOn: (src: string, target: string) => void;
}) {
  const nav = useNavigate();
  const [over, setOver] = useState(false);
  const { row } = node;
  const st = stage(row);
  const closed = collapsed.has(row.path);
  return (
    <>
      <div
        className={`tree-row ${selected === row.path ? "selected" : ""} ${over ? "drop" : ""}`}
        style={{ paddingLeft: 4 + depth * 14 }}
        title={row.hypothesis || row.path}
        draggable
        onDragStart={(e) => e.dataTransfer.setData("text/featx-node", row.path)}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOver(false);
          const src = e.dataTransfer.getData("text/featx-node");
          if (src && src !== row.path) onDropOn(src, row.path);
        }}
        onClick={() => nav(`/node/${row.path}`)}
      >
        <span
          className="twist"
          onClick={(e) => {
            e.stopPropagation();
            toggle(row.path);
          }}
        >
          {node.children.length ? (closed ? "▸" : "▾") : ""}
        </span>
        <VerdictMark verdict={row.verdict} />
        <span className={`name ${row.status === "abandoned" ? "abandoned" : ""}`}>{row.name}</span>
        <span className="meta">
          {row.worktree && <span title="has a git worktree">⎇</span>}
          {row.status !== "active" && row.status !== "abandoned" && <span>{row.status}</span>}
          {st && <Badge kind={st.state}>{st.text}</Badge>}
          {row.active.length > 0 && row.eta_s !== null && <span title="estimated time remaining">{fmtDuration(row.eta_s)}</span>}
        </span>
      </div>
      {!closed && node.children.map((c) => <Row key={c.row.path} node={c} depth={depth + 1} selected={selected} collapsed={collapsed} toggle={toggle} onDropOn={onDropOn} />)}
    </>
  );
}

export function TreePanel() {
  const { t } = useI18n();
  const loc = useLocation();
  const nav = useNavigate();
  const tree = useQuery({ queryKey: ["tree"], queryFn: () => api.get<TreeRow[]>("/tree"), refetchInterval: 5000 });
  const [filter, setFilter] = usePersistentState("tree:filter", "");
  const [collapsedList, setCollapsedList] = usePersistentState<string[]>("tree:collapsed", []);
  const collapsed = useMemo(() => new Set(collapsedList), [collapsedList]);
  const [adding, setAdding] = useState(false);
  const [move, setMove] = useState<{ src: string; dst: string } | null>(null);
  const rows = tree.data ?? [];
  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    if (!f) return rows;
    const hit = rows.filter((r) => r.path.toLowerCase().includes(f) || r.hypothesis.toLowerCase().includes(f)).map((r) => r.path);
    // keep the ancestors of every hit so the hierarchy stays readable
    return rows.filter((r) => hit.some((h) => h === r.path || h.startsWith(`${r.path}/`)));
  }, [rows, filter]);
  const roots = useMemo(() => buildTree(shown), [shown]);
  const m = loc.pathname.match(/^\/(node|runset)\/(.+)$/);
  const selected = m ? (m[1] === "runset" ? m[2].replace(/\/[^/]+$/, "") : m[2]) : "";
  const toggle = (path: string) => setCollapsedList((old) => (old.includes(path) ? old.filter((p) => p !== path) : [...old, path]));
  const base = (p: string) => p.slice(p.lastIndexOf("/") + 1);
  return (
    <div
      style={{ minHeight: "100%" }}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        const src = e.dataTransfer.getData("text/featx-node");
        if (src && src.includes("/")) setMove({ src, dst: base(src) });
      }}
    >
      <div className="tree-tools">
        <input type="text" placeholder={t("filter nodes")} value={filter} onChange={(e) => setFilter(e.target.value)} />
        <button title={t("add a category")} onClick={() => setAdding(true)}>
          +
        </button>
      </div>
      <ErrorBox error={tree.error} />
      {roots.map((n) => (
        <Row key={n.row.path} node={n} depth={0} selected={selected} collapsed={collapsed} toggle={toggle} onDropOn={(src, target) => setMove({ src, dst: `${target}/${base(src)}` })} />
      ))}
      {!tree.isLoading && !rows.length && <div className="muted" style={{ padding: 8 }}>{t("no nodes yet: add a category")}</div>}
      <div className="muted small" style={{ padding: "10px 6px" }}>
        Drag a node onto another to move it (a dry run is shown first).
      </div>
      {adding && <AddNodeDialog parent="" onClose={() => setAdding(false)} onDone={(p) => { setAdding(false); nav(`/node/${p}`); }} />}
      {move && (
        <MoveDialog
          src={move.src}
          initialDst={move.dst}
          onClose={() => setMove(null)}
          onDone={(dst) => {
            setMove(null);
            if (selected === move.src || selected.startsWith(`${move.src}/`)) nav(`/node/${dst}${selected.slice(move.src.length)}`);
          }}
        />
      )}
    </div>
  );
}
