import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { setToken } from "./api";

const now = Date.now() / 1000;
const FIXTURES: Record<string, unknown> = {
  "/health": { ok: true, host: "node024", pid: 1, started: "2026-10-12T10:00:00", last_tick: now, root: "/r" },
  "/status": { started: "", last_tick: now, tick_errors: [], tasks: { queued: 1, running: 2, succeeded: 3, failed: 0, cancelled: 0 }, jobs_per_host: {}, unacked_events: 4 },
  "/hosts": [{ host: "node024", featx_jobs: 2, free_cores: 40, free_mem_gb: 300, cores: 128, load1: 80, bad: false }],
  "/tree": [
    { id: "1", path: "wpe", name: "wpe", depth: 0, kind: "category", verdict: "open", status: "active", hypothesis: "", worktree: false, runsets: 0, latest: null, tasks: {}, active: [], eta_s: null },
    { id: "2", path: "wpe/overlay", name: "overlay", depth: 1, kind: "feature", verdict: "supported", status: "active", hypothesis: "h", worktree: true, runsets: 2, latest: "20261012-2", tasks: { run: "running" }, active: ["run"], eta_s: 600 },
  ],
  "/nodes/context": {
    node: { id: "1", path: "wpe", kind: "category", hypothesis: "", verdict: "open", verdict_note: "", status: "active", evidence: [], tags: [] },
    dir: "/r/tree/wpe", ancestors: [], children: [], notes: "", git: null, recipes: {}, own_recipes: {},
    scripts_dir: "/r/tree/wpe/scripts", scripts: [], runsets: [], runset_count: 0,
  },
  "/config": { path: "/r/featx.yaml", user_text: "", user: {}, defaults_text: "", restart_keys: [],
    effective: { workloads: { spec06: { description: "SPEC06", label: "spec06", checkpoint: "spec06_base" }, spec17: { description: "SPEC17", label: "spec17", checkpoint: "spec17_o2" } }, default_workload: "spec06" } },
  "/checkpoints": [
    { name: "spec06_base", path: "/ck/spec06_base", spec: "06", label: "spec06", layout: "legacy0", multihart: false, json: "/j", has_insts: true, benches: 29, missing: 0, extra: 0, restorer_needed: false, error: "", n_points: 1090, runnable: true, traced_by: ["spec06"] },
    { name: "spec17_new", path: "/ck/spec17_new", spec: "17", label: "spec17", layout: "modern", multihart: false, json: "/j", has_insts: true, benches: 20, missing: 0, extra: 0, restorer_needed: false, error: "", n_points: 1783, runnable: true, traced_by: [] },
    { name: "spec17_2harts", path: "/ck/spec17_2harts", spec: "17", label: "spec17", layout: "modern", multihart: true, json: "/j", has_insts: true, benches: 20, missing: 0, extra: 0, restorer_needed: false, error: "", n_points: 10, runnable: false, traced_by: [] },
  ],
  "/doctor": { checks: [{ check: "data root", status: "ok", detail: "/r" }, { check: "git.repo", status: "warn", detail: "unset" }], counts: { ok: 1, warn: 1, fail: 0 }, ok: true },
  "/help": { path: "/featx/docs/reference.md", topics: [{ topic: "concepts", title: "Nodes" }, { topic: "run", title: "Run recipe keys" }] },
  "/help/run": { topic: "run", text: "## run — Run recipe keys\n\n| key | default |\n|---|---|\n| `mode` | `null` |\n" },
  "/leaderboard": {
    metric: "pk.self.mean_abs_err_pct",
    order: "asc",
    class: "k1",
    metrics: [{ name: "pk.self.mean_abs_err_pct", runsets: 2 }],
    classes: [{ key: "k1", label: "pk vs overlay-0928 · W=20M I=20M · 3 pts", step: "pk", reference: "overlay-0928", W: 20000000, I: 20000000, n_points: 3, rows: 2, complete_rows: 2 }],
    rows: [
      { runset: "wpe/overlay/20261012-2", node: "wpe/overlay", name: "20261012-2", created: "2026-10-12T17:00:00", purpose: "best", sealed: true, verdict: "supported", node_status: "active", bin: "emu-a", value: 0.8, class: "k1", complete: true, points: 3, common_points: 3, tasks: {}, rank: 1 },
    ],
  },
  "/result": { ref: "wpe/overlay/20261013-1", id: "R1", purpose: "", created: "2026-10-13T10:00:00", sealed: false,
    derived_from: null, dir: "/r/tree/wpe/overlay/20261013-1", post: {}, problem_log_tail: "",
    tasks: [{ id: "T1", kind: "run", name: "run", state: "succeeded", counts: { succeeded: 1090 }, failure: null }] },
  "/tasks/T1": { id: "T1", kind: "run", node: "wpe/overlay", runset: "20261013-1", state: "succeeded", note: "", after: [],
    counts: { succeeded: 1090 }, failure: null, created: "", blocked: false, design_relaunches: 0, outputs: {}, eta_s: null,
    jobs: Array.from({ length: 1090 }, (_, i) => ({ name: `bench_${i}_0.001000`, state: "succeeded", host: "node024", attempt: 1,
      rc: 0, progress: {}, failure: null, eta_s: null })) },
  "/tasks/T1/log": { path: "/r/log", exists: true, tail: "done" },
};

// per-test replacements of FIXTURES entries
let extra: Record<string, unknown> = {};

beforeEach(() => {
  extra = {};
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.split("?")[0];
      if (init?.method && init.method !== "GET") {
        return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json" } });
      }
      const body = path in extra ? extra[path] : FIXTURES[path];
      return new Response(JSON.stringify(body ?? { error: `no fixture ${path}` }), {
        status: body === undefined ? 404 : 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

function writes(): [string, Record<string, unknown>][] {
  const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls;
  return calls.filter(([, init]) => init?.method && init.method !== "GET").map(([u, init]) => [u.split("?")[0], JSON.parse(String(init!.body ?? "{}"))]);
}

const CTX = FIXTURES["/nodes/context"] as { node: Record<string, unknown> } & Record<string, unknown>;
const withNode = (node: Record<string, unknown>, more: Record<string, unknown> = {}) => ({ ...CTX, ...more, node: { ...CTX.node, ...node } });
const GIT = { repo: "/repo", base: "main", branch: "wpe", worktree: "/wt/wpe" };
const CHANGES = {
  repos: {
    "": { path: "/wt/wpe", base: "main", exists: true, merge_base: "abc123", ahead: 1, behind: 0, added: 3, deleted: 1,
      commits: [{ sha: "d00d1e", author: "me", when: "1 hour ago", subject: "try a wider window" }],
      files: [{ status: "M", path: "src/core.scala", old_path: null, added: 3, deleted: 1, uncommitted: false }], untracked: [] },
  },
  total: { files: 1, added: 3, deleted: 1, ahead: 1 },
};

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("App", () => {
  it("asks for the token first", () => {
    renderAt("/leaderboard");
    expect(screen.getByText(/token/i, { selector: "h1, h2, p, label" })).toBeTruthy();
  });

  it("renders the tree, status and the ranked leaderboard", async () => {
    setToken("t0k");
    renderAt("/leaderboard");
    expect(await screen.findByText("20261012-2", { exact: false })).toBeTruthy();
    expect(await screen.findByText("overlay")).toBeTruthy();
    expect(await screen.findByText(/2 running · 1 queued/)).toBeTruthy();
    expect(screen.getByText("Events (4)")).toBeTruthy();
    expect(screen.getByText("0.800")).toBeTruthy();
    const call = (fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls.find(([u]) => u.startsWith("/leaderboard"))!;
    expect((call[1].headers as Record<string, string>)["X-Featx-Token"]).toBe("t0k");
  });

  it("does not show a creation form just for opening the worktree tab", async () => {
    setToken("t0k");
    renderAt("/node/wpe?tab=worktree");
    const open = await screen.findByRole("button", { name: "Create worktree…" });
    expect(screen.queryByPlaceholderText(/git.repo/)).toBeNull();
    fireEvent.click(open);
    expect(await screen.findByPlaceholderText(/git.repo/)).toBeTruthy();
    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls;
    expect(calls.filter(([, init]) => init?.method && init.method !== "GET")).toEqual([]);
  });

  it("keeps a single node header when leaving the scripts tab", async () => {
    setToken("t0k");
    renderAt("/node/wpe?tab=scripts");
    await screen.findByText(/Child nodes inherit them/);
    fireEvent.click(screen.getByRole("button", { name: "worktree" }));
    await screen.findByRole("button", { name: "Create worktree…" });
    expect(document.querySelectorAll(".crumbs").length).toBe(1);
  });

  it("turns the submit presets into overrides and the CLI line", async () => {
    setToken("t0k");
    renderAt("/node/wpe?tab=submit");
    await screen.findByText("spec17 — SPEC17");
    const select = (option: string) => screen.getAllByRole("combobox").find((el) => el.textContent?.includes(option))!;
    fireEvent.change(select("exec"), { target: { value: "exec" } });
    const workload = select("spec17");
    fireEvent.change(workload, { target: { value: "spec17" } });
    expect(await screen.findByText(/--set build.mode=exec --set run.mode=exec --set run.workload=spec17/, { selector: "span" })).toBeTruthy();
    // exec: a checkpoint set of the workload's SPEC version, multi-hart sets left out
    const ckpt = await waitFor(() => {
      const el = select("spec17_new (1783 points)");
      if (!el) throw new Error("no checkpoint select yet");
      return el;
    });
    expect(ckpt.textContent).toContain("inherit (spec17_o2 — the workload's)");
    expect(ckpt.textContent).not.toContain("spec06_base");
    expect(ckpt.textContent).not.toContain("2harts");
    fireEvent.change(ckpt, { target: { value: "spec17_new" } });
    expect(await screen.findByText(/--set run.checkpoint=spec17_new/, { selector: "span" })).toBeTruthy();
  });

  it("lists checkpoint sets and the doctor report in settings", async () => {
    setToken("t0k");
    renderAt("/settings");
    expect(await screen.findByText("spec17_new")).toBeTruthy();
    expect(screen.queryByText("spec17_2harts")).toBeNull();
    fireEvent.click(screen.getByLabelText(/multi-hart \/ broken/));
    expect(await screen.findByText("multi-hart (not runnable)")).toBeTruthy();
    expect(await screen.findByText("1 ok, 1 warn, 0 fail")).toBeTruthy();
  });

  it("scrolls a long jobs table in its own box", async () => {
    setToken("t0k");
    renderAt("/runset/wpe/overlay/20261013-1?job=bench_1000_0.001000");
    await screen.findByText("bench_1089_0.001000");
    const box = screen.getByTestId("jobs-scroll");
    expect(box.classList.contains("table-wrap")).toBe(true);
    expect(box.querySelectorAll("tbody tr").length).toBe(1090);
    expect(box.querySelector("tr.selected td")?.textContent).toBe("bench_1000_0.001000");
    expect(await screen.findByText("done")).toBeTruthy();   // the log below is still rendered
  });

  it("shows a docs topic", async () => {
    setToken("t0k");
    renderAt("/docs/run");
    expect(await screen.findByRole("heading", { name: "run — Run recipe keys" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "concepts" })).toBeTruthy();
    expect(document.querySelector(".docs-body table")).toBeTruthy();
  });

  it("edits and removes evidence entries as a whole list", async () => {
    setToken("t0k");
    extra["/nodes/context"] = withNode({ evidence: ["wpe/overlay/20261012-2", "note a"] });
    renderAt("/node/wpe");
    fireEvent.click(await screen.findByRole("button", { name: "remove evidence 2" }));
    await waitFor(() => expect(writes()).toEqual([["/nodes/update", { ref: "wpe", evidence: ["wpe/overlay/20261012-2"] }]]));
    fireEvent.click(screen.getAllByRole("button", { name: "edit" })[0]);
    fireEvent.change(screen.getByLabelText("edit evidence"), { target: { value: "wpe/overlay/20261012-3" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Save" }).find((b) => !(b as HTMLButtonElement).disabled)!);
    await waitFor(() => expect(writes()[1]).toEqual(["/nodes/update", { ref: "wpe", evidence: ["wpe/overlay/20261012-3", "note a"] }]));
    fireEvent.change(screen.getByPlaceholderText(/Ctrl\+Enter adds/), { target: { value: "new note" } });
    fireEvent.keyDown(screen.getByPlaceholderText(/Ctrl\+Enter adds/), { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(writes()[2]).toEqual(["/nodes/update", { ref: "wpe", add_evidence: ["new note"] }]));
  });

  it("saves the comment", async () => {
    setToken("t0k");
    extra["/nodes/context"] = withNode({ comment: "old idea" });
    renderAt("/node/wpe");
    const box = await screen.findByDisplayValue("old idea");
    fireEvent.change(box, { target: { value: "maybe the prefetcher" } });
    expect(screen.getByText("unsaved")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Save" }).find((b) => !(b as HTMLButtonElement).disabled)!);
    await waitFor(() => expect(writes()).toEqual([["/nodes/update", { ref: "wpe", comment: "maybe the prefetcher" }]]));
  });

  it("edits the notes", async () => {
    setToken("t0k");
    renderAt("/node/wpe?tab=notes");
    await screen.findByText("No notes yet — Edit to add.");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("notes"), { target: { value: "# plan\n- a" } });
    expect(await screen.findByRole("heading", { name: "plan" })).toBeTruthy();   // live preview
    fireEvent.click(screen.getByRole("button", { name: "Save notes" }));
    await waitFor(() => expect(writes()).toEqual([["/nodes/update", { ref: "wpe", notes: "# plan\n- a" }]]));
  });

  it("changes a queued task's priority and the core caps", async () => {
    setToken("t0k");
    extra["/status"] = { ...(FIXTURES["/status"] as object), cores_in_use: 40, max_total_cores: 300, run_cores_in_use: 32, build_cores_in_use: 8, max_build_cores: 64 };
    extra["/tasks"] = [
      { id: "T2", kind: "run", node: "wpe/overlay", runset: "20261013-2", state: "queued", note: "", after: [], counts: { queued: 10 }, failure: null, created: "2026-10-13T11:00:00", priority: 0, blocked: false, design_relaunches: 0, outputs: {}, eta_s: null },
      { id: "T3", kind: "run", node: "wpe/overlay", runset: "20261013-3", state: "queued", note: "", after: [], counts: { queued: 10 }, failure: null, created: "2026-10-13T12:00:00", priority: 5, blocked: false, design_relaunches: 0, outputs: {}, eta_s: null },
    ];
    renderAt("/tasks");
    expect(await screen.findByText("cores 32/300")).toBeTruthy();
    // launch order: the younger task with priority 5 is listed first
    const rows = [...document.querySelectorAll("tbody tr td.mono a")].map((a) => a.textContent);
    expect(rows.slice(0, 2)).toEqual(["wpe/overlay/20261013-3", "wpe/overlay/20261013-2"]);
    fireEvent.click(screen.getByRole("button", { name: "raise priority of T2" }));
    await waitFor(() => expect(writes()).toEqual([["/tasks/T2/priority", { priority: 1, runset: true }]]));
    fireEvent.change(screen.getByLabelText("placement.max_total_cores"), { target: { value: "200" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Set" }).find((b) => !(b as HTMLButtonElement).disabled)!);
    await waitFor(() => expect(writes()[1]).toEqual(["/config/set", { key: "placement.max_total_cores", value: 200 }]));
  });

  it("remembers the submit form, coverage and spec-score included", async () => {
    setToken("t0k");
    const first = renderAt("/node/wpe?tab=submit");
    await screen.findByText("spec17 — SPEC17");
    const select = (option: string) => screen.getAllByRole("combobox").find((el) => el.textContent?.includes(option))!;
    fireEvent.change(select("full (all points)"), { target: { value: "0.3" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "spec-score" }));
    fireEvent.change(screen.getByPlaceholderText("0"), { target: { value: "3" } });
    const cli = /--priority 3 --set run.coverage=0.3 --set post.steps.spec-score.plugin=spec-score/;
    expect(await screen.findByText(cli, { selector: "span" })).toBeTruthy();
    first.unmount();
    renderAt("/node/wpe?tab=submit");
    expect(await screen.findByText(cli, { selector: "span" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "reset form" }));
    expect(screen.queryByText(cli, { selector: "span" })).toBeNull();
  });

  it("shows the worktree's code changes and a file diff", async () => {
    setToken("t0k");
    extra["/nodes/context"] = withNode({}, { git: GIT });
    extra["/worktrees/status"] = { worktree: "/wt/wpe", branch: "wpe", exists: true, head: "d00d1e", dirty: 0 };
    extra["/worktrees/changes"] = CHANGES;
    extra["/worktrees/diff"] = { sub: "", path: "src/core.scala", diff: "--- a/src/core.scala\n+++ b/src/core.scala\n@@ -1 +1 @@\n-old\n+new", truncated: false };
    renderAt("/node/wpe?tab=worktree");
    expect(await screen.findByText(/code changed: 1 file, \+3 −1, 1 commit ahead/)).toBeTruthy();
    expect(await screen.findByText("try a wider window")).toBeTruthy();
    fireEvent.click(screen.getByText("src/core.scala"));
    expect((await screen.findByText("+new")).className).toBe("add");
    expect(screen.getByText("-old").className).toBe("del");
    expect(writes()).toEqual([]);
  });
});
