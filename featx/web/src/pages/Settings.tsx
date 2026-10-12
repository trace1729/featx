import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api";
import { ErrorBox, Loading, useAction } from "../components/ui";
import { YamlEditor } from "../components/YamlEditor";
import type { Baselines, ConfigView } from "../types";

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
  const act = useAction();
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
              <td>{b.description}</td>
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
    </div>
  );
}

function BinImportCard() {
  const [path, setPath] = useState("");
  const [label, setLabel] = useState("");
  const [res, setRes] = useState<{ key: string; emu: string } | null>(null);
  const act = useAction();
  return (
    <div>
      <div className="row">
        <input type="text" className="mono" placeholder="/abs/path/to/emu" style={{ width: 460 }} value={path} onChange={(e) => setPath(e.target.value)} />
        <input type="text" placeholder="label" style={{ width: 200 }} value={label} onChange={(e) => setLabel(e.target.value)} />
        <button disabled={!path || act.busy} onClick={() => act.run(async () => setRes(await api.post("/bins/import", { path, label })))}>
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

export function SettingsPage() {
  return (
    <div>
      <h1>Settings</h1>
      <h2>featx.yaml</h2>
      <ConfigEditor />
      <h2>Baselines</h2>
      <BaselinesCard />
      <h2>Import a binary into the cache</h2>
      <BinImportCard />
    </div>
  );
}
