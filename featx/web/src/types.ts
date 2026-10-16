// Shapes of the featxd REST responses the UI reads.

export type Verdict = "open" | "supported" | "refuted" | "inconclusive" | string;

export interface TreeRow {
  id: string;
  path: string;
  name: string;
  depth: number;
  kind: string;
  verdict: Verdict;
  status: string;
  hypothesis: string;
  worktree: boolean;
  runsets: number;
  latest: string | null;
  tasks: Record<string, string>;
  active: string[];
  eta_s: number | null;
}

export interface NodeSummary {
  id: string;
  path: string;
  kind: string;
  hypothesis: string;
  verdict: Verdict;
  verdict_note: string;
  status: string;
  evidence: string[];
  tags: string[];
  comment?: string;
}

export interface TaskBrief {
  id: string;
  kind: string;
  name: string;
  state: string;
  priority?: number;
  counts: Record<string, number>;
  failure: unknown;
}

export interface RunsetSummary {
  ref: string;
  id: string;
  purpose: string;
  created: string;
  sealed: boolean;
  derived_from: string | null;
  dir: string;
  tasks: TaskBrief[];
  post: Record<string, Record<string, unknown>>;
  problem_log_tail: string;
  bin?: Record<string, unknown> | null;
  provenance?: Record<string, unknown>;
  reports?: Record<string, string>;
}

export interface GitInfo {
  repo: string;
  base: string;
  branch: string;
  worktree: string;
  submodule_worktrees?: Record<string, { repo: string; branch: string; path: string }>;
}

export interface NodeContext {
  node: NodeSummary;
  dir: string;
  ancestors: NodeSummary[];
  children: NodeSummary[];
  notes: string;
  git: GitInfo | null;
  recipes: Record<string, Record<string, unknown>>;
  own_recipes: Record<string, Record<string, unknown>>;
  scripts_dir: string;
  scripts: string[];
  runsets: RunsetSummary[];
  runset_count: number;
}

export interface JobView {
  name: string;
  state: string;
  host: string | null;
  attempt: number;
  rc: number | null;
  progress: { inst?: number; inst_per_s?: number; mtime?: number } & Record<string, unknown>;
  failure: { signature?: string; class?: string; tail?: string } & Record<string, unknown> | null;
  eta_s: number | null;
}

export interface TaskView {
  id: string;
  kind: string;
  node: string;
  runset: string;
  state: string;
  note: string;
  after: string[];
  counts: Record<string, number>;
  failure: unknown;
  created: string;
  priority?: number;
  blocked: boolean;
  design_relaunches: number;
  outputs: Record<string, unknown>;
  eta_s: number | null;
  progress_pct?: number | null;
  target_inst?: number;
  jobs?: JobView[];
  spec?: Record<string, unknown>;
}

export interface Status {
  started: string;
  last_tick: number;
  tick_errors: string[];
  tasks: Record<string, number>;
  jobs_per_host: Record<string, number>;
  unacked_events: number;
  cores_in_use?: number;
  max_total_cores?: number;
  run_cores_in_use?: number;
  build_cores_in_use?: number;
  max_build_cores?: number;
}

export interface Health {
  ok: boolean;
  host: string;
  pid: number;
  started: string;
  last_tick: number;
  root: string;
}

export interface HostRow {
  host: string;
  state?: string;
  featx_jobs?: number;
  free_cores?: number;
  free_mem_gb?: number;
  cores?: number;
  load1?: number;
  bad?: boolean;
  error?: string;
}

export interface EventRow {
  seq: number;
  time: string;
  type: string;
  task?: string;
  kind?: string;
  node?: string;
  runset?: string;
  state?: string;
  job?: string;
  signature?: string;
  failure?: unknown;
  acked: boolean;
  [key: string]: unknown;
}

export interface BoardClass {
  key: string;
  label: string;
  step: string;
  reference: string;
  W: number | null;
  I: number | null;
  n_points: number;
  rows: number;
  complete_rows: number;
}

export interface BoardRow {
  runset: string;
  node: string;
  name: string;
  created: string;
  purpose: string;
  sealed: boolean;
  verdict: Verdict;
  node_status: string;
  bin: string | null;
  value: number;
  class: string;
  complete: boolean;
  points: number | null;
  common_points: number | null;
  tasks: Record<string, string>;
  rank: number | null;
}

export interface Board {
  metric: string | null;
  order: "asc" | "desc";
  class: string | null;
  metrics: { name: string; runsets: number }[];
  classes: BoardClass[];
  rows: BoardRow[];
}

export interface ConfigView {
  path: string;
  user_text: string;
  user: Record<string, unknown>;
  defaults_text: string;
  effective: Record<string, unknown>;
  restart_keys: string[];
}

export interface FileRead {
  path: string;
  size: number;
  truncated: boolean;
  text: string;
}

export interface FileList {
  root: string;
  files: { path: string; size: number; mtime: number; preview: boolean }[];
  truncated: boolean;
}

export interface MovePlan {
  dry_run?: boolean;
  from: string;
  to: string;
  create_parents?: string[];
  nodes: {
    from: string;
    to: string;
    kind?: string;
    runsets?: number;
    git?: {
      worktree?: { from: string; to: string };
      worktree_kept?: string;
      branch?: { from: string; to: string; submodules?: string[] };
      error?: string;
    };
  }[];
}

export interface StorageReport {
  root: string;
  nodes: { path: string; bytes: number; human: string; runsets: number; worktree_human?: string }[];
  bincache: { key: string; bytes: number; human: string; label?: string; refs: string[] }[];
  trash: { path: string; bytes: number; human: string }[];
  collect_cache_bytes: number;
  total_bytes: number;
  total_human: string;
}

export interface GcReport {
  applied: boolean;
  candidates: { kind: string; path: string; bytes: number; human: string; node?: string }[];
  total_bytes: number;
  total_human: string;
}

export type Baselines = Record<string, { root: string; description?: string; binary?: string; checkpoint?: string }>;

/** GET /checkpoints: one SimPoint checkpoint set (run.checkpoint for exec runs). */
export interface CheckpointSet {
  name: string;
  path: string;
  spec: string | null;
  label: string | null;
  layout: string | null;
  multihart: boolean;
  json: string | null;
  has_insts: boolean;
  benches: number;
  missing: number;
  extra: number;
  restorer_needed: boolean;
  error: string;
  n_points: number;
  runnable: boolean;
  traced_by: string[];
}

export interface DoctorReport {
  checks: { check: string; status: "ok" | "warn" | "fail"; detail: string }[];
  counts: Record<string, number>;
  ok: boolean;
}

export interface DocsIndex {
  path: string;
  topics: { topic: string; title: string }[];
}

export interface DocsTopic {
  topic: string;
  text: string;
}
