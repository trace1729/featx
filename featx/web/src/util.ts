import { useEffect, useState } from "react";
import type { TreeRow } from "./types";

export function fmtDuration(s: number | null | undefined): string {
  if (s === null || s === undefined || !isFinite(s)) return "?";
  if (s <= 0) return "0s";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, "0")}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(s / 86400)}d${String(Math.floor((s % 86400) / 3600)).padStart(2, "0")}h`;
}

export function fmtBytes(n: number): string {
  const units = ["B", "K", "M", "G", "T"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return i === 0 ? `${v}B` : `${v.toFixed(1)}${units[i]}`;
}

export function fmtCount(n: number | null | undefined): string {
  if (n === null || n === undefined) return "-";
  if (n >= 1e6 && n % 1e6 === 0) return `${n / 1e6}M`;
  if (n >= 1e3 && n % 1e3 === 0) return `${n / 1e3}k`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e4) return `${(n / 1e3).toFixed(1)}k`;
  return String(Math.round(n));
}

/** Metric values: percentages and ratios with enough digits to tell runs apart, integers as is. */
export function fmtValue(v: unknown): string {
  if (typeof v !== "number") return v === null || v === undefined ? "" : String(v);
  if (Number.isInteger(v)) return v.toLocaleString("en-US");
  const a = Math.abs(v);
  if (a >= 100) return v.toFixed(1);
  if (a >= 1) return v.toFixed(3);
  return v.toPrecision(3);
}

export function metricLabel(name: string): string {
  const [step, ...rest] = name.split(".");
  const key = rest.join(".");
  const known: Record<string, string> = {
    "self.mean_abs_err_pct": "IPC mean |err| %",
    "self.mean_err_pct": "IPC mean err %",
    "self.max_abs_err_pct": "IPC max |err| %",
    "self.ipc_geomean": "IPC geomean",
    "self.finished": "finished points",
  };
  return known[key] ? `${known[key]} (${step})` : name;
}

export function fmtTime(iso: string | undefined): string {
  if (!iso) return "";
  return iso.replace("T", " ").replace(/([+-]\d\d:\d\d|Z)$/, "").slice(5, 16);
}

export function ago(ts: number | undefined): string {
  if (!ts) return "never";
  const d = Date.now() / 1000 - ts;
  return d < 2 ? "now" : `${fmtDuration(d)} ago`;
}

export interface TreeNode {
  row: TreeRow;
  children: TreeNode[];
}

/** /tree returns flat rows sorted by path; rebuild the hierarchy. Rows whose parent is
 * missing (filtered subtree) become roots. */
export function buildTree(rows: TreeRow[]): TreeNode[] {
  const byPath = new Map<string, TreeNode>();
  const roots: TreeNode[] = [];
  for (const row of [...rows].sort((a, b) => a.path.localeCompare(b.path))) {
    const node: TreeNode = { row, children: [] };
    byPath.set(row.path, node);
    const cut = row.path.lastIndexOf("/");
    const parent = cut > 0 ? byPath.get(row.path.slice(0, cut)) : undefined;
    (parent ? parent.children : roots).push(node);
  }
  return roots;
}

/** A runset ref is <node path>/<YYYYMMDD-n>. */
export function splitRunset(ref: string): { node: string; name: string } {
  const cut = ref.lastIndexOf("/");
  return { node: ref.slice(0, cut), name: ref.slice(cut + 1) };
}

export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        cell += '"';
        i += 1;
      } else if (c === '"') quoted = false;
      else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") {
      row.push(cell);
      cell = "";
    } else if (c === "\n") {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else if (c !== "\r") cell += c;
  }
  if (cell !== "" || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

/** --set style overrides ("run.W=1000000", one per line) -> {run: {W: 1000000}}. */
export function parseOverrides(text: string): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const line of text.split(/\n/).map((l) => l.trim()).filter(Boolean)) {
    const eq = line.indexOf("=");
    if (eq < 0) throw new Error(`bad override "${line}" (want kind.key=value)`);
    const parts = line.slice(0, eq).trim().split(".");
    if (parts.length < 2 || !["build", "run", "post"].includes(parts[0])) {
      throw new Error(`override "${line}" must start with build., run. or post.`);
    }
    const raw = line.slice(eq + 1).trim();
    let value: unknown = raw;
    if (/^-?\d+(\.\d+)?$/.test(raw)) value = Number(raw);
    else if (raw === "true" || raw === "false") value = raw === "true";
    else if (raw === "null") value = null;
    else if (/^[[{]/.test(raw)) {
      try {
        value = JSON.parse(raw);
      } catch {
        value = raw.replace(/^\[|\]$/g, "").split(",").map((s) => s.trim()).filter(Boolean);
      }
    }
    let cur = out;
    for (const p of parts.slice(0, -1)) {
      cur[p] = (cur[p] as Record<string, unknown>) ?? {};
      cur = cur[p] as Record<string, unknown>;
    }
    cur[parts[parts.length - 1]] = value;
  }
  return out;
}

export const STATE_ORDER = ["running", "launching", "queued", "failed", "cancelled", "succeeded"];

export function countsText(counts: Record<string, number>): string {
  return STATE_ORDER.filter((s) => counts[s]).map((s) => `${counts[s]} ${s}`).join(" · ");
}

/** Submit-form presets ("" = inherit the recipe's value). */
export interface QuickPresets {
  tool: string;
  mode: string;
  chiseldb: string;
  chiselmap: string;
  workload: string;
  /** exec runs: a checkpoint set name (run.checkpoint) */
  checkpoint: string;
  W: string;
  I: string;
  /** "" inherit, "full" = every point (run.coverage null), else a fraction in (0, 1] */
  coverage: string;
  /** "on": add the spec-score post step (perf_trigger --report scoring) */
  specScore: string;
}

export const NO_PRESETS: QuickPresets = { tool: "", mode: "", chiseldb: "", chiselmap: "", workload: "", checkpoint: "", W: "", I: "", coverage: "", specScore: "" };

/** Presets -> recipe overrides for the selected stages. The mode goes to the build when it is
 * built in this submit (the run follows it), and to the run as well so a --bin run checks it. */
export function presetOverrides(q: QuickPresets, kinds: string[]): Record<string, Record<string, unknown>> {
  const out: Record<string, Record<string, unknown>> = {};
  const put = (kind: string, path: string[], value: unknown) => {
    if (!kinds.includes(kind)) return;
    let cur = (out[kind] ??= {});
    for (const p of path.slice(0, -1)) cur = (cur[p] ??= {}) as Record<string, unknown>;
    cur[path[path.length - 1]] = value;
  };
  if (q.tool) put("build", ["tool"], q.tool);
  if (q.mode) {
    put("build", ["mode"], q.mode);
    put("run", ["mode"], q.mode);
  }
  for (const opt of ["chiseldb", "chiselmap"] as const) {
    if (q[opt]) put("build", ["options", opt], q[opt] === "on");
  }
  if (q.workload) put("run", ["workload"], q.workload);
  if (q.checkpoint) put("run", ["checkpoint"], q.checkpoint);
  for (const key of ["W", "I"] as const) {
    const v = q[key].replace(/[_,]/g, "").trim();
    if (v) put("run", [key], /^\d+$/.test(v) ? Number(v) : v);
  }
  const cov = (q.coverage ?? "").trim();
  if (cov === "full") put("run", ["coverage"], null);
  else if (cov) put("run", ["coverage"], /^\d*\.?\d+$/.test(cov) ? Number(cov) : cov);
  if (q.specScore === "on") put("post", ["steps", "spec-score", "plugin"], "spec-score");
  return out;
}

/** Recursive merge (later wins; arrays and scalars replace), as featx merges recipe layers. */
export function deepMerge(a: Record<string, unknown>, b: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...a };
  for (const [k, v] of Object.entries(b)) {
    const prev = out[k];
    const isObj = (x: unknown) => typeof x === "object" && x !== null && !Array.isArray(x);
    out[k] = isObj(prev) && isObj(v) ? deepMerge(prev as Record<string, unknown>, v as Record<string, unknown>) : v;
  }
  return out;
}

/** Overrides -> the equivalent `featx submit --set kind.key=value` flags. */
/** Runnable checkpoint sets by SPEC version ("06", "17", ...), only `spec`'s when given. */
export function checkpointGroups<T extends { spec: string | null; runnable: boolean; name: string }>(sets: T[], spec?: string | null): [string, T[]][] {
  const groups = new Map<string, T[]>();
  for (const s of sets) {
    if (!s.runnable || (spec && s.spec !== spec)) continue;
    const key = s.spec ?? "?";
    groups.set(key, [...(groups.get(key) ?? []), s]);
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => [k, v.sort((x, y) => x.name.localeCompare(y.name))]);
}

export function setFlags(overrides: Record<string, unknown>): string[] {
  const out: string[] = [];
  const walk = (prefix: string, v: unknown) => {
    if (typeof v === "object" && v !== null && !Array.isArray(v) && Object.keys(v).length) {
      for (const [k, x] of Object.entries(v)) walk(prefix ? `${prefix}.${k}` : k, x);
    } else {
      const text = typeof v === "string" ? v : JSON.stringify(v);
      out.push(`--set ${/[\s"'$\\[\]{}]/.test(`${prefix}=${text}`) ? `'${prefix}=${text}'` : `${prefix}=${text}`}`);
    }
  };
  walk("", overrides);
  return out;
}

const STORE_PREFIX = "featx.v1.";

export function loadStored<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(STORE_PREFIX + key);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function saveStored(key: string, value: unknown) {
  try {
    if (value === undefined) localStorage.removeItem(STORE_PREFIX + key);
    else localStorage.setItem(STORE_PREFIX + key, JSON.stringify(value));
  } catch {
    // storage full or disabled: the choice is just not remembered
  }
}

/** useState that survives reloads (localStorage, JSON, key prefixed "featx.v1."). Changing
 * `key` loads that key's value; `fallback` is used when nothing is stored under it. */
export function usePersistentState<T>(key: string, fallback: T): [T, (v: T | ((old: T) => T)) => void] {
  const [state, setState] = useState<{ key: string; value: T }>(() => ({ key, value: loadStored(key, fallback) }));
  const current = state.key === key ? state.value : loadStored(key, fallback);
  useEffect(() => {
    if (state.key !== key) setState({ key, value: loadStored(key, fallback) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const set = (v: T | ((old: T) => T)) =>
    setState((old) => {
      const base = old.key === key ? old.value : loadStored(key, fallback);
      const value = typeof v === "function" ? (v as (o: T) => T)(base) : v;
      saveStored(key, value);
      return { key, value };
    });
  return [current, set];
}
