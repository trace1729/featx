import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import YAML from "yaml";
import { api } from "../api";
import { AddNodeDialog, MoveDialog, RemoveDialog } from "../components/NodeDialogs";
import { Markdown } from "../components/FileViewer";
import { Badge, Dialog, ErrorBox, Loading, StateBadge, Tabs, VerdictMark, useAction } from "../components/ui";
import { YamlEditor } from "../components/YamlEditor";
import type { NodeContext, RunsetSummary } from "../types";
import { fmtTime, fmtValue, metricLabel, parseCsv, parseOverrides } from "../util";

const VERDICTS = ["open", "supported", "refuted", "inconclusive"];
const STATUSES = ["active", "parked", "done", "abandoned"];
const KINDS = ["build", "run", "post"] as const;
const TABS = ["runsets", "submit", "recipes", "scripts", "worktree", "notes"] as const;
type Tab = (typeof TABS)[number];

function useInvalidateNode() {
  const qc = useQueryClient();
  return () => Promise.all([qc.invalidateQueries({ queryKey: ["node"] }), qc.invalidateQueries({ queryKey: ["tree"] })]);
}

/** The few numbers worth a column: every post step's self.* error/ipc keys. */
function keyMetrics(rs: RunsetSummary): [string, unknown][] {
  const out: [string, unknown][] = [];
  for (const [step, s] of Object.entries(rs.post ?? {})) {
    for (const k of ["self.mean_abs_err_pct", "self.ipc_geomean"]) {
      if (k in s) out.push([`${step}.${k}`, s[k]]);
    }
  }
  return out;
}

function RunsetsTab({ ctx }: { ctx: NodeContext }) {
  if (!ctx.runsets.length) return <div className="muted">no runsets yet: use the submit tab.</div>;
  return (
    <div>
      <table>
        <thead>
          <tr>
            <th>runset</th>
            <th>created</th>
            <th>purpose</th>
            <th>tasks</th>
            <th>key metrics</th>
          </tr>
        </thead>
        <tbody>
          {ctx.runsets.map((rs) => (
            <tr key={rs.ref}>
              <td className="mono">
                <Link to={`/runset/${rs.ref}`}>{rs.ref.slice(ctx.node.path.length + 1)}</Link> {rs.sealed && <Badge kind="sealed">sealed</Badge>}
                {rs.derived_from && <div className="muted small">from {rs.derived_from}</div>}
              </td>
              <td className="muted">{fmtTime(rs.created)}</td>
              <td>{rs.purpose}</td>
              <td>
                {rs.tasks.map((t) => (
                  <span key={t.id} style={{ marginRight: 4 }}>
                    {t.kind}: <StateBadge state={t.state} />
                  </span>
                ))}
              </td>
              <td className="small">
                {keyMetrics(rs).map(([k, v]) => (
                  <div key={k}>
                    {metricLabel(k)} <b className="mono">{fmtValue(v)}</b>
                  </div>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {ctx.runset_count > ctx.runsets.length && (
        <p className="muted">
          showing the latest {ctx.runsets.length} of {ctx.runset_count}
        </p>
      )}
    </div>
  );
}

function SubmitTab({ ctx }: { ctx: NodeContext }) {
  const nav = useNavigate();
  const qc = useQueryClient();
  const act = useAction();
  const [kinds, setKinds] = useState<string[]>(["build", "run", "post"]);
  const [bin, setBin] = useState("");
  const [points, setPoints] = useState("");
  const [postSteps, setPostSteps] = useState("");
  const [overrides, setOverrides] = useState("");
  const [purpose, setPurpose] = useState("");
  let parsed: Record<string, unknown> = {};
  let parseError: string | null = null;
  try {
    parsed = parseOverrides(overrides);
  } catch (e) {
    parseError = (e as Error).message;
  }
  const list = (s: string) => parseCsv(s.replace(/\s+/g, ","))[0]?.filter(Boolean) ?? [];
  const submit = () =>
    act.run(async () => {
      const r = await api.post<{ runset: string }>("/tasks", {
        node: ctx.node.path,
        kinds: KINDS.filter((k) => kinds.includes(k)),
        purpose,
        overrides: parsed,
        bin: bin || undefined,
        points: points.trim() ? list(points) : undefined,
        post_steps: postSteps.trim() ? list(postSteps) : undefined,
      });
      await qc.invalidateQueries();
      nav(`/runset/${r.runset}`);
    });
  return (
    <div className="form">
      <label>stages</label>
      <div className="row">
        {KINDS.map((k) => (
          <label key={k}>
            <input type="checkbox" checked={kinds.includes(k)} onChange={(e) => setKinds(e.target.checked ? [...kinds, k] : kinds.filter((x) => x !== k))} /> {k}
          </label>
        ))}
        {!kinds.includes("build") && kinds.includes("run") && <span className="muted small">without build the run recipe's emu (or --bin) is used</span>}
      </div>
      <label>purpose</label>
      <input type="text" value={purpose} onChange={(e) => setPurpose(e.target.value)} placeholder="what this runset is meant to show" />
      <label>binary</label>
      <input type="text" className="mono" value={bin} onChange={(e) => setBin(e.target.value)} placeholder="optional: bincache key, runset ref, or /abs/path/emu" />
      <label>points</label>
      <input type="text" className="mono" value={points} onChange={(e) => setPoints(e.target.value)} placeholder="optional subset, comma or space separated" />
      <label>post steps</label>
      <input type="text" className="mono" value={postSteps} onChange={(e) => setPostSteps(e.target.value)} placeholder="optional subset of the post recipe's steps" />
      <label>overrides</label>
      <div>
        <textarea className="mono" rows={4} value={overrides} onChange={(e) => setOverrides(e.target.value)} placeholder={"run.W=20000000\nrun.I=20000000"} />
        {parseError ? <div className="error">{parseError}</div> : overrides.trim() && <pre className="mono small">{JSON.stringify(parsed)}</pre>}
      </div>
      <span />
      <div>
        <button className="primary" disabled={act.busy || !!parseError || !kinds.length} onClick={submit}>
          Submit
        </button>
        <span className="muted small"> same as: featx submit {ctx.node.path} {KINDS.filter((k) => kinds.includes(k)).join(" ")}</span>
        <ErrorBox error={act.error} />
      </div>
    </div>
  );
}

function RecipesTab({ ctx }: { ctx: NodeContext }) {
  const [kind, setKind] = useState<(typeof KINDS)[number]>("run");
  const own = ctx.own_recipes[kind] ?? {};
  const ownText = Object.keys(own).length ? YAML.stringify(own) : "";
  const [text, setText] = useState(ownText);
  const [explain, setExplain] = useState(false);
  const act = useAction();
  const invalidate = useInvalidateNode();
  useEffect(() => setText(ownText), [kind, ownText]);
  const ex = useQuery({
    queryKey: ["explain", ctx.node.path, kind],
    queryFn: () => api.get<string>("/recipes", { ref: ctx.node.path, kind, explain: true }),
    enabled: explain,
  });
  const save = () =>
    act.run(async () => {
      await api.post("/recipes", { ref: ctx.node.path, kind, data: text });
      await invalidate();
    });
  return (
    <div>
      <div className="row">
        <Tabs tabs={KINDS} value={kind} onChange={setKind} />
        <label>
          <input type="checkbox" checked={explain} onChange={(e) => setExplain(e.target.checked)} /> explain (which layer set each key)
        </label>
      </div>
      <div className="grid2">
        <div>
          <h3>This node's layer</h3>
          <YamlEditor value={text} onChange={setText} height="360px" />
          <div className="row" style={{ marginTop: 6 }}>
            <button className="primary" disabled={act.busy || text === ownText} onClick={save}>
              Save {kind} recipe
            </button>
            <button disabled={text === ownText} onClick={() => setText(ownText)}>
              Revert
            </button>
            <span className="muted small">stored as YAML, comments are not kept · featx recipe set {ctx.node.path} {kind} -f FILE</span>
          </div>
          <ErrorBox error={act.error} />
        </div>
        <div>
          <h3>Effective (root → ancestors → node)</h3>
          {explain ? (
            ex.isLoading ? <Loading /> : <pre className="logbox">{ex.data}</pre>
          ) : (
            <YamlEditor value={YAML.stringify(ctx.recipes[kind] ?? {})} readOnly height="360px" />
          )}
          <ErrorBox error={ex.error} />
        </div>
      </div>
    </div>
  );
}

function ScriptsTab({ ctx }: { ctx: NodeContext }) {
  const [sel, setSel] = useState<string | null>(ctx.scripts[0] ?? null);
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const act = useAction();
  const invalidate = useInvalidateNode();
  const q = useQuery({
    queryKey: ["script", ctx.node.path, sel],
    queryFn: () => api.get<string>("/scripts", { ref: ctx.node.path, name: sel }),
    enabled: !!sel,
  });
  useEffect(() => {
    if (sel && q.data !== undefined) {
      setName(sel);
      setText(q.data);
    }
  }, [sel, q.data]);
  const save = () =>
    act.run(async () => {
      await api.post("/scripts", { ref: ctx.node.path, name, content: text });
      setSel(name);
      await invalidate();
    });
  return (
    <div>
      <p className="muted small mono">{ctx.scripts_dir} · recipes reach them as ${"{scripts}"}/NAME</p>
      <div className="chips">
        {ctx.scripts.map((s) => (
          <button key={s} className={`chip ${s === sel ? "active" : ""}`} onClick={() => setSel(s)}>
            {s}
          </button>
        ))}
        <button
          className="chip"
          onClick={() => {
            setSel(null);
            setName("");
            setText("#!/bin/bash\nset -euo pipefail\n");
          }}
        >
          + new
        </button>
      </div>
      <div className="row" style={{ margin: "8px 0" }}>
        <input type="text" className="mono" value={name} onChange={(e) => setName(e.target.value)} placeholder="script name" />
        <button className="primary" disabled={!name || act.busy} onClick={save}>
          Save (executable)
        </button>
      </div>
      <textarea className="mono" rows={18} style={{ width: "100%" }} value={text} onChange={(e) => setText(e.target.value)} />
      <ErrorBox error={act.error ?? q.error} />
    </div>
  );
}

interface WtStatus {
  worktree: string;
  branch: string;
  exists: boolean;
  head?: string;
  dirty?: number;
  submodules?: Record<string, { branch: string; path: string; head: string; dirty: number }>;
}

function CreateWorktreeDialog({ path, onClose }: { path: string; onClose: () => void }) {
  const act = useAction();
  const invalidate = useInvalidateNode();
  const [repo, setRepo] = useState("");
  const [base, setBase] = useState("");
  return (
    <Dialog open onClose={onClose} title={`Create a worktree for ${path}`}>
      <div className="form">
        <label>repo</label>
        <input type="text" className="mono" autoFocus value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="default: git.repo from featx.yaml" />
        <label>base</label>
        <input type="text" className="mono" value={base} onChange={(e) => setBase(e.target.value)} placeholder="default: git.base from featx.yaml" />
      </div>
      <p className="muted small">Creates the worktree and branch featx/{path} (and the configured submodule worktrees). This takes a while.</p>
      <ErrorBox error={act.error} />
      <div className="dialog-actions">
        <button onClick={onClose}>Cancel</button>
        <button
          className="primary"
          disabled={act.busy}
          onClick={() =>
            act.run(async () => {
              await api.post("/worktrees", { ref: path, repo: repo || undefined, base: base || undefined });
              await invalidate();
              onClose();
            })
          }
        >
          {act.busy ? "Creating…" : "Create"}
        </button>
      </div>
    </Dialog>
  );
}

function WorktreeTab({ ctx }: { ctx: NodeContext }) {
  const [creating, setCreating] = useState(false);
  const q = useQuery({
    queryKey: ["wt", ctx.node.path],
    queryFn: () => api.get<WtStatus>("/worktrees/status", { ref: ctx.node.path }),
    enabled: !!ctx.git,
  });
  if (!ctx.git) {
    // ${worktree} only resolves from the node's own git info (recipes.base_context), so a
    // build of this node is refused until it has one; runs with --bin do not need it.
    return (
      <div>
        <p>
          No worktree. Builds of this node need one (<span className="mono">{"${worktree}"}</span>); runs with an
          existing binary (<span className="mono">--bin</span>) don't.
        </p>
        <div className="row">
          <button onClick={() => setCreating(true)}>Create worktree…</button>
          <span className="muted small">featx worktree create {ctx.node.path}</span>
        </div>
        {creating && <CreateWorktreeDialog path={ctx.node.path} onClose={() => setCreating(false)} />}
      </div>
    );
  }
  const g = ctx.git;
  return (
    <div>
      <div className="kv">
        <span>worktree</span>
        <span className="mono">{g.worktree}</span>
        <span>branch</span>
        <span className="mono">{g.branch}</span>
        <span>repo / base</span>
        <span className="mono">
          {g.repo} @ {g.base}
        </span>
        {q.data && (
          <>
            <span>head</span>
            <span className="mono">{q.data.head}</span>
            <span>state</span>
            <span>
              {!q.data.exists ? (
                <Badge kind="failed">missing on disk</Badge>
              ) : q.data.dirty ? (
                <Badge kind="warn">{q.data.dirty} uncommitted change(s)</Badge>
              ) : (
                <Badge kind="succeeded">clean</Badge>
              )}
            </span>
          </>
        )}
      </div>
      {q.data?.submodules && Object.keys(q.data.submodules).length > 0 && (
        <>
          <h3>submodule worktrees</h3>
          <pre className="mono">{YAML.stringify(q.data.submodules)}</pre>
        </>
      )}
      <ErrorBox error={q.error} />
    </div>
  );
}

function Header({ ctx }: { ctx: NodeContext }) {
  const n = ctx.node;
  const nav = useNavigate();
  const act = useAction();
  const invalidate = useInvalidateNode();
  const [hyp, setHyp] = useState(n.hypothesis);
  const [note, setNote] = useState(n.verdict_note);
  const [evidence, setEvidence] = useState("");
  const [dialog, setDialog] = useState<"add" | "mv" | "rm" | null>(null);
  useEffect(() => {
    setHyp(n.hypothesis);
    setNote(n.verdict_note);
  }, [n.hypothesis, n.verdict_note]);
  const update = (fields: Record<string, unknown>) =>
    act.run(async () => {
      await api.post("/nodes/update", { ref: n.path, ...fields });
      await invalidate();
    });
  return (
    <div>
      <div className="crumbs">
        {ctx.ancestors.map((a) => (
          <span key={a.path}>
            <Link to={`/node/${a.path}`}>{a.path.split("/").pop()}</Link> /{" "}
          </span>
        ))}
      </div>
      <div className="row">
        <h1 style={{ margin: 0 }}>
          <VerdictMark verdict={n.verdict} /> <span className="mono">{n.path}</span>
        </h1>
        <Badge>{n.kind}</Badge>
        {n.tags.map((t) => (
          <Badge key={t} kind="tag">
            #{t}
          </Badge>
        ))}
        <span className="spacer" />
        <button onClick={() => setDialog("add")}>Add child…</button>
        <button onClick={() => setDialog("mv")}>Move…</button>
        <button className="danger" onClick={() => setDialog("rm")}>
          Remove…
        </button>
      </div>
      <div className="card form" style={{ marginTop: 10 }}>
        <label>hypothesis</label>
        <div className="row">
          <textarea rows={2} style={{ flex: 1 }} value={hyp} onChange={(e) => setHyp(e.target.value)} />
          <button disabled={hyp === n.hypothesis || act.busy} onClick={() => update({ hypothesis: hyp })}>
            Save
          </button>
        </div>
        <label>verdict</label>
        <div className="row">
          <select value={n.verdict} onChange={(e) => update({ verdict: e.target.value })}>
            {VERDICTS.map((v) => (
              <option key={v}>{v}</option>
            ))}
          </select>
          <input type="text" style={{ flex: 1 }} value={note} placeholder="verdict note" onChange={(e) => setNote(e.target.value)} />
          <button disabled={note === n.verdict_note || act.busy} onClick={() => update({ verdict_note: note })}>
            Save note
          </button>
          <label>status</label>
          <select value={n.status} onChange={(e) => update({ status: e.target.value })}>
            {STATUSES.map((v) => (
              <option key={v}>{v}</option>
            ))}
          </select>
        </div>
        <label>evidence</label>
        <div>
          {n.evidence.map((ev) => (
            <div key={ev} className="mono small">
              {ev.includes("/") && !ev.includes(" ") ? <Link to={`/runset/${ev}`}>{ev}</Link> : ev}
            </div>
          ))}
          <div className="row">
            <input type="text" className="mono" style={{ flex: 1 }} value={evidence} placeholder="runset ref or note" onChange={(e) => setEvidence(e.target.value)} />
            <button disabled={!evidence.trim() || act.busy} onClick={() => update({ add_evidence: [evidence.trim()] }).then(() => setEvidence(""))}>
              Add evidence
            </button>
          </div>
        </div>
      </div>
      <ErrorBox error={act.error} />
      {dialog === "add" && <AddNodeDialog parent={n.path} onClose={() => setDialog(null)} onDone={(p) => nav(`/node/${p}`)} />}
      {dialog === "mv" && <MoveDialog src={n.path} onClose={() => setDialog(null)} onDone={(dst) => nav(`/node/${dst}`, { replace: true })} />}
      {dialog === "rm" && (
        <RemoveDialog
          path={n.path}
          onClose={() => setDialog(null)}
          onDone={(r) => {
            const parent = n.path.includes("/") ? `/node/${n.path.slice(0, n.path.lastIndexOf("/"))}` : "/leaderboard";
            nav(parent, { replace: true, state: { notice: `Removed ${n.path}: ${JSON.stringify(r)}` } });
          }}
        />
      )}
    </div>
  );
}

export function NodePage() {
  const path = useParams()["*"] ?? "";
  const notice = (useLocation().state as { notice?: string } | null)?.notice;
  const [sp, setSp] = useSearchParams();
  const tab = (TABS as readonly string[]).includes(sp.get("tab") ?? "") ? (sp.get("tab") as Tab) : "runsets";
  const q = useQuery({
    queryKey: ["node", path],
    queryFn: () => api.get<NodeContext>("/nodes/context", { ref: path, max_runsets: 50 }),
    refetchInterval: 10000,
  });
  if (q.isLoading) return <Loading what={path} />;
  if (q.error) return <ErrorBox error={q.error} />;
  const ctx = q.data!;
  return (
    <div>
      {notice && <div className="okbox mono small">{notice}</div>}
      {/* sibling keys must differ: a shared key made React leave a stale Header behind when ScriptsTab unmounted */}
      <Header key={`header:${ctx.node.id}`} ctx={ctx} />
      <Tabs tabs={TABS} value={tab} onChange={(t) => setSp({ tab: t }, { replace: true })} />
      {tab === "runsets" && <RunsetsTab ctx={ctx} />}
      {tab === "submit" && <SubmitTab ctx={ctx} />}
      {tab === "recipes" && <RecipesTab ctx={ctx} />}
      {tab === "scripts" && <ScriptsTab key={`scripts:${ctx.node.id}`} ctx={ctx} />}
      {tab === "worktree" && <WorktreeTab ctx={ctx} />}
      {tab === "notes" && (ctx.notes ? <Markdown text={ctx.notes} /> : <div className="muted">no notes.md in {ctx.dir}</div>)}
      {ctx.children.length > 0 && (
        <>
          <h2>Children</h2>
          <ul>
            {ctx.children.map((c) => (
              <li key={c.path}>
                <VerdictMark verdict={c.verdict} /> <Link to={`/node/${c.path}`}>{c.path}</Link> <span className="muted">{c.hypothesis}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
