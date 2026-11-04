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

export function metricLabel(name: string, translate: (text: string) => string = (s) => s): string {
  const [step, ...rest] = name.split(".");
  const key = rest.join(".");
  const known: Record<string, string> = {
    "self.mean_abs_err_pct": "IPC mean |err| %",
    "self.mean_err_pct": "IPC mean err %",
    "self.max_abs_err_pct": "IPC max |err| %",
    "self.ipc_geomean": "IPC geomean",
    "self.finished": "finished points",
  };
  return known[key] ? `${translate(known[key])} (${step})` : name;
}

export function fmtTime(iso: string | undefined): string {
  if (!iso) return "";
  return iso.replace("T", " ").replace(/([+-]\d\d:\d\d|Z)$/, "").slice(5, 16);
}

export function shortCommit(sha: string | null | undefined): string {
  return (sha ?? "").slice(0, 12);
}

/** Copy text to the clipboard. The UI may be served over plain http, where navigator.clipboard
 * does not exist, so fall back to a hidden textarea. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the textarea */
  }
  try {
    const el = document.createElement("textarea");
    el.value = text;
    el.setAttribute("readonly", "");
    el.style.position = "fixed";
    el.style.opacity = "0";
    document.body.appendChild(el);
    el.select();
    const ok = document.execCommand?.("copy") ?? false;
    document.body.removeChild(el);
    return ok;
  } catch {
    return false;
  }
}

export function ago(ts: number | undefined, translate: (text: string) => string = (s) => s): string {
  if (!ts) return translate("never");
  const d = Date.now() / 1000 - ts;
  return d < 2 ? translate("now") : translate("{duration} ago").replace("{duration}", fmtDuration(d));
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

export function countsText(counts: Record<string, number>, translate: (text: string) => string = (s) => s): string {
  return STATE_ORDER.filter((s) => counts[s]).map((s) => `${counts[s]} ${translate(s)}`).join(" · ");
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

// ------------------------------------------------------- untracked folding
export interface UntrackedDir { type: "dir"; name: string; path: string; count: number; children: UntrackedNode[] }
export interface UntrackedFile { type: "file"; name: string; path: string }
export type UntrackedNode = UntrackedDir | UntrackedFile;

interface RawDir { dirs: Map<string, RawDir>; files: string[] }

export function countUntracked(nodes: UntrackedNode[]): number {
  return nodes.reduce((n, x) => n + (x.type === "file" ? 1 : x.count), 0);
}

function dirNode(name: string, path: string, raw: RawDir): UntrackedDir {
  // single-child chains collapse into one row: a/b/c instead of a, b, c
  while (raw.dirs.size === 1 && raw.files.length === 0) {
    const [childName, child] = [...raw.dirs.entries()][0];
    name = `${name}/${childName}`;
    path = `${path}/${childName}`;
    raw = child;
  }
  const children: UntrackedNode[] = [
    ...[...raw.dirs.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([child, dir]) => dirNode(child, `${path}/${child}`, dir)),
    ...raw.files
      .slice()
      .sort()
      .map((file) => ({ type: "file" as const, name: file.slice(file.lastIndexOf("/") + 1), path: file })),
  ];
  return { type: "dir", name, path, count: countUntracked(children), children };
}

/** Untracked paths -> a directory tree (folded single-child chains), dirs before files. */
export function foldUntracked(paths: string[]): UntrackedNode[] {
  const root: RawDir = { dirs: new Map(), files: [] };
  for (const path of paths) {
    const parts = path.split("/");
    let cur = root;
    for (const dir of parts.slice(0, -1)) {
      let next = cur.dirs.get(dir);
      if (!next) {
        next = { dirs: new Map(), files: [] };
        cur.dirs.set(dir, next);
      }
      cur = next;
    }
    cur.files.push(path);
  }
  return [
    ...[...root.dirs.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([name, dir]) => dirNode(name, name, dir)),
    ...root.files
      .slice()
      .sort()
      .map((file) => ({ type: "file" as const, name: file, path: file })),
  ];
}

/** How many distinct directories below the root the untracked files sit in. */
export function untrackedDirCount(paths: string[]): number {
  return new Set(paths.filter((p) => p.includes("/")).map((p) => p.slice(0, p.lastIndexOf("/")))).size;
}

// ------------------------------------------------------------- overrides
/** Leaf keys of the raw submit overrides: {run: {W: 1}} -> ["run.W"]. */
export function overrideKeys(overrides: Record<string, unknown> | undefined): Set<string> {
  const out = new Set<string>();
  const walk = (prefix: string, v: unknown) => {
    if (typeof v === "object" && v !== null && !Array.isArray(v)) {
      for (const [k, x] of Object.entries(v)) walk(prefix ? `${prefix}.${k}` : k, x);
    } else if (prefix) out.add(prefix);
  };
  walk("", overrides ?? {});
  return out;
}

/** One value of the compact parameters line: `key` identifies it across runsets (for muting
 * values every runset shares), `text` is what the cell shows. */
export interface ParamToken {
  key: string;
  text: string;
}

const PARAM_STAGES = ["build", "run", "post"] as const;
export type ParamStage = (typeof PARAM_STAGES)[number];

function short(v: unknown): string {
  if (typeof v === "number") return Number.isInteger(v) ? fmtCount(v) : String(v);
  if (Array.isArray(v)) return v.map(short).join(",");
  if (typeof v === "object" && v !== null) return JSON.stringify(v);
  return String(v);
}

/** The headline parameters (`RunsetSummary.params`) as a few short tokens per stage:
 * build `gsim · trace · tracertl · chiseldb`, run `spec06 · exec · W=20M · I=40M · 143 pt`,
 * post `pk · score(spec-score)`. A run-only runset's build line names its binary instead. */
export function paramTokens(
  params: Record<string, Record<string, unknown>> | undefined,
  code?: { from?: string | null; bin_key?: string | null; bin_emu?: string | null; imported?: boolean } | null,
): Record<ParamStage, ParamToken[]> {
  const out: Record<ParamStage, ParamToken[]> = { build: [], run: [], post: [] };
  const b = params?.build ?? {};
  if (Object.keys(b).length) {
    for (const k of ["tool", "mode", "pipeline"]) if (b[k] !== undefined) out.build.push({ key: k, text: short(b[k]) });
    for (const [k, v] of Object.entries(b)) {
      if (!k.startsWith("options.")) continue;
      const name = k.slice("options.".length);
      out.build.push({ key: k, text: v === true ? name : v === false ? `no-${name}` : `${name}=${short(v)}` });
    }
  } else if (code?.from) {
    out.build.push({ key: "bin", text: `bin ← ${code.from.split("/").pop()}` });
  } else if (code?.bin_key) {
    out.build.push({ key: "bin", text: `${code.imported ? "imported " : ""}bin ${String(code.bin_key).slice(0, 8)}` });
  } else if (code?.bin_emu) {
    out.build.push({ key: "bin", text: `bin …/${code.bin_emu.split("/").slice(-2).join("/")}` });
  }
  const r = params?.run ?? {};
  if (Object.keys(r).length) {
    const wl = r.workload ?? (typeof r.suite === "string" ? r.suite.split("/").pop() : undefined);
    if (wl !== undefined) out.run.push({ key: "workload", text: short(wl) });
    if (r.mode !== undefined) out.run.push({ key: "mode", text: short(r.mode) });
    if (r.checkpoint !== undefined && r.checkpoint !== r.workload) out.run.push({ key: "checkpoint", text: `ckpt ${short(r.checkpoint)}` });
    for (const k of ["W", "I"]) if (r[k] !== undefined) out.run.push({ key: k, text: `${k}=${short(r[k])}` });
    if (r.points !== undefined) out.run.push({ key: "points", text: `${short(r.points)} pt` });
    if (r.coverage !== undefined) out.run.push({ key: "coverage", text: `cov ${short(r.coverage)}` });
    const env = r.env && typeof r.env === "object" ? Object.entries(r.env as Record<string, unknown>) : [];
    for (const [k, v] of env) out.run.push({ key: `env.${k}`, text: `${k}=${short(v)}` });
  }
  const p = params?.post ?? {};
  for (const [k, v] of Object.entries(p)) {
    if (!k.endsWith(".plugin")) continue;
    const step = k.slice(0, -".plugin".length);
    out.post.push({ key: k, text: v === step ? step : `${step}(${short(v)})` });
  }
  return out;
}

/** Token keys (per stage) whose text is the same in every runset that has the stage: the cell
 * mutes them so what differs between the node's runsets stands out. */
export function sharedTokens(all: Record<ParamStage, ParamToken[]>[]): Set<string> {
  const shared = new Set<string>();
  for (const stage of PARAM_STAGES) {
    const rows = all.map((t) => t[stage]).filter((t) => t.length);
    if (rows.length < 2) continue;
    const keys = new Set(rows.flatMap((t) => t.map((x) => x.key)));
    for (const key of keys) {
      const texts = rows.map((t) => t.find((x) => x.key === key)?.text ?? null);
      if (texts.every((x) => x === texts[0])) shared.add(`${stage}.${key}`);
    }
  }
  return shared;
}

/** Differing recipe keys the headline tokens already show (`build.tool` for the tool token...). */
export function coveredByTokens(key: string): boolean {
  return /^(build\.(tool|mode|pipeline|options\.)|run\.(workload|suite|mode|checkpoint|W|I|coverage|benchmarks|points|env\.)|post\.steps\.[^.]+\.plugin$)/.test(key);
}
