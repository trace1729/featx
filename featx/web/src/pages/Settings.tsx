import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api";
import { Badge, ErrorBox, Loading, useAction } from "../components/ui";
import { YamlEditor } from "../components/YamlEditor";
import type { Baselines, CheckpointSet, ConfigView, DoctorReport } from "../types";

function ConfigEditor() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["config"], queryFn: () => api.get<ConfigView>("/config"), staleTime: Infinity });
  const [text, setText] = useState<string | null>(null);
  const [saved, setSaved] = useState<{ changed: string[]; restart_needed: string[] } | null>(null);
  const act = useAction();
  useEffect(() => {
    if (q.data && text === null) setText(q.data.user_text);
  }, [q.data, text]);
  if (q.isLoading || text === null) return <Loading what="config" />;
  if (q.error) return <ErrorBox error={q.error} />;
  const c = q.data!;
  const save = () =>
    act.run(async () => {
      setSaved(await api.put("/config", { text }));
      await qc.invalidateQueries({ queryKey: ["config"] });
    });
  return (
    <div>
      <div className="grid2">
        <div>
          <h3>
            Overrides <span className="muted mono small">{c.path}</span>
          </h3>
          <YamlEditor value={text} onChange={setText} height="420px" />
        </div>
        <div>
          <h3>Package defaults (read only)</h3>
          <YamlEditor value={c.defaults_text} readOnly height="420px" />
        </div>
      </div>
      <p className="muted small">
        Only keys that exist in the defaults are accepted. A key set to <code>null</code> falls back to the default. Changes to {c.restart_keys.join(", ")} need a
        daemon restart, everything else applies at the next scheduler tick.
      </p>
      <ErrorBox error={act.error} />
      {saved && (
        <div className={saved.restart_needed.length ? "notice" : "okbox"}>
          Saved. Changed: {saved.changed.join(", ") || "nothing"}.
          {saved.restart_needed.length > 0 && ` Restart featxd to apply: ${saved.restart_needed.join(", ")} (featx daemon stop && featx daemon start).`}
        </div>
      )}
      <div className="row">
        <button className="primary" disabled={act.busy || text === c.user_text} onClick={save}>
          Save
        </button>
        <button
          disabled={text === c.user_text}
          onClick={() => {
            setText(c.user_text);
            act.clear();
          }}
        >
          Revert
        </button>
      </div>
    </div>
  );
}

function BaselinesCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["baselines"], queryFn: () => api.get<Baselines>("/baselines") });
  const [form, setForm] = useState({ name: "", root: "", description: "", binary: "" });
  const [blocked, setBlocked] = useState<string | null>(null);
  const act = useAction();
  const rm = useAction();
  const remove = (name: string, force = false) =>
    rm.run(async () => {
      setBlocked(name);
      await api.del(`/baselines/${encodeURIComponent(name)}`, { force });
      setBlocked(null);
      await qc.invalidateQueries({ queryKey: ["baselines"] });
    });
  const add = () =>
    act.run(async () => {
      await api.post("/baselines", { ...form, binary: form.binary || undefined });
      setForm({ name: "", root: "", description: "", binary: "" });
      await qc.invalidateQueries({ queryKey: ["baselines"] });
    });
  const field = (key: keyof typeof form, placeholder: string, width: number) => (
    <input type="text" className="mono" placeholder={placeholder} style={{ width }} value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} />
  );
  return (
    <div>
      <ErrorBox error={q.error} />
      <table>
        <thead>
          <tr>
            <th>name</th>
            <th>result root</th>
            <th>description</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {Object.entries(q.data ?? {}).map(([name, b]) => (
            <tr key={name}>
              <td className="mono">{name}</td>
              <td className="mono small">
                {b.root}
                {b.binary && <div className="muted">bin {b.binary}</div>}
              </td>
              <td>
                {b.description}
                {b.checkpoint && <div className="muted small mono">checkpoint {b.checkpoint}</div>}
              </td>
              <td>
                <button className="link" disabled={rm.busy} onClick={() => remove(name)} title="refused while a workload or a post recipe uses it">
                  remove
                </button>
                {blocked === name && !!rm.error && !rm.busy && (
                  <button className="link danger" onClick={() => remove(name, true)}>
                    remove anyway
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row" style={{ marginTop: 8 }}>
        {field("name", "name", 130)}
        {field("root", "/abs/result/root", 360)}
        {field("description", "description", 220)}
        {field("binary", "binary (optional)", 220)}
        <button disabled={!form.name || !form.root || act.busy} onClick={add}>
          Add baseline
        </button>
      </div>
      <ErrorBox error={act.error} />
      <ErrorBox error={rm.error} />
    </div>
  );
}

function BinImportCard() {
  const [path, setPath] = useState("");
  const [label, setLabel] = useState("");
  const [mode, setMode] = useState("");
  const [res, setRes] = useState<{ key: string; emu: string } | null>(null);
  const act = useAction();
  return (
    <div>
      <div className="row">
        <input type="text" className="mono" placeholder="/abs/path/to/emu" style={{ width: 460 }} value={path} onChange={(e) => setPath(e.target.value)} />
        <input type="text" placeholder="label" style={{ width: 200 }} value={label} onChange={(e) => setLabel(e.target.value)} />
        <select value={mode} onChange={(e) => setMode(e.target.value)} title="the binary's TRACERTL_MODE; a run with --bin <key> follows it">
          <option value="">mode: unknown</option>
          <option value="trace">trace</option>
          <option value="exec">exec</option>
        </select>
        <button disabled={!path || act.busy} onClick={() => act.run(async () => setRes(await api.post("/bins/import", { path, label, mode: mode || undefined })))}>
          {act.busy ? "Copying…" : "Import"}
        </button>
      </div>
      <ErrorBox error={act.error} />
      {res && (
        <div className="okbox mono">
          {res.key} → {res.emu}
        </div>
      )}
    </div>
  );
}

const CHECK_BADGE: Record<string, string> = { ok: "succeeded", warn: "warn", fail: "failed" };

/** featx doctor: what the configuration points at, and whether the hosts see the install. */
function DoctorCard() {
  const [ssh, setSsh] = useState(false);
  const q = useQuery({ queryKey: ["doctor", ssh], queryFn: () => api.get<DoctorReport>("/doctor", { ssh }), staleTime: 60000 });
  return (
    <div>
      <div className="row">
        {q.data && (
          <span>
            {q.data.counts.ok ?? 0} ok, {q.data.counts.warn ?? 0} warn, {q.data.counts.fail ?? 0} fail
          </span>
        )}
        <button disabled={q.isFetching} onClick={() => (ssh ? q.refetch() : setSsh(true))} title="also ssh to every host (slow)">
          {q.isFetching ? "Checking…" : ssh ? "Recheck with hosts" : "Check hosts too"}
        </button>
        <span className="muted small">same as: featx doctor{ssh ? "" : " --no-ssh"}</span>
      </div>
      <ErrorBox error={q.error} />
      {q.isLoading ? (
        <Loading what="doctor" />
      ) : (
        <table>
          <tbody>
            {(q.data?.checks ?? []).map((c) => (
              <tr key={c.check}>
                <td>
                  <Badge kind={CHECK_BADGE[c.status]}>{c.status}</Badge>
                </td>
                <td>{c.check}</td>
                <td className="mono small">{c.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** The SimPoint checkpoint sets under checkpoints.roots (run.checkpoint of exec runs). */
function CheckpointsCard() {
  const [all, setAll] = useState(false);
  const [refresh, setRefresh] = useState(false);
  const q = useQuery({ queryKey: ["checkpoints", refresh], queryFn: () => api.get<CheckpointSet[]>("/checkpoints", refresh ? { refresh } : undefined), staleTime: 300000 });
  const rows = (q.data ?? []).filter((c) => all || c.runnable);
  return (
    <div>
      <div className="row">
        <label>
          <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> also multi-hart / broken sets
        </label>
        <button disabled={q.isFetching} onClick={() => (refresh ? q.refetch() : setRefresh(true))} title="rebuild every set's cached index (featx ckpt rescan)">
          {q.isFetching ? "Scanning…" : "Rescan"}
        </button>
        <span className="muted small">exec runs pick one with run.checkpoint (submit tab, mode exec)</span>
      </div>
      <ErrorBox error={q.error} />
      {q.isLoading ? (
        <Loading what="checkpoint sets" />
      ) : (
        <table>
          <thead>
            <tr>
              <th>set</th>
              <th>layout</th>
              <th>points</th>
              <th>benches</th>
              <th>insts</th>
              <th>restorer</th>
              <th>traced by / problem</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.path}>
                <td className="mono" title={c.path}>
                  {c.name}
                </td>
                <td>{c.layout ?? "-"}</td>
                <td className="mono">
                  {c.n_points}
                  {(c.missing > 0 || c.extra > 0) && <span className="muted small" title="weights json entries without an image / images not in the json"> ({c.missing} missing, {c.extra} extra)</span>}
                </td>
                <td className="mono">{c.benches}</td>
                <td>{c.has_insts ? "yes" : "no"}</td>
                <td>{c.restorer_needed ? ".gz" : "-"}</td>
                <td className="small">
                  {c.runnable ? c.traced_by.join(", ") : <Badge kind={c.multihart ? "warn" : "failed"}>{[c.multihart && "multi-hart (not runnable)", c.error].filter(Boolean).join("; ")}</Badge>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function SettingsPage() {
  return (
    <div>
      <h1>Settings</h1>
      <h2>featx.yaml</h2>
      <ConfigEditor />
      <h2>Doctor</h2>
      <DoctorCard />
      <h2>Baselines</h2>
      <BaselinesCard />
      <h2>Checkpoint sets</h2>
      <CheckpointsCard />
      <h2>Import a binary into the cache</h2>
      <BinImportCard />
    </div>
  );
}
