import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { setToken } from "./api";

const now = Date.now() / 1000;
const FIXTURES: Record<string, unknown> = {
  "/health": { ok: true, host: "worker-a", pid: 1, started: "2026-10-12T10:00:00", last_tick: now, root: "/r" },
  "/status": { started: "", last_tick: now, tick_errors: [], tasks: { queued: 1, running: 2, succeeded: 3, failed: 0, cancelled: 0 }, jobs_per_host: {}, unacked_events: 4 },
  "/hosts": [{ host: "worker-a", featx_jobs: 2, free_cores: 40, free_mem_gb: 300, cores: 128, load1: 80, bad: false }],
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
    jobs: Array.from({ length: 1090 }, (_, i) => ({ name: `bench_${i}_0.001000`, state: "succeeded", host: "worker-a", attempt: 1,
      rc: 0, progress: {}, failure: null, eta_s: null })) },
  "/tasks/T1/log": { path: "/r/log", exists: true, tail: "done" },
};

// jsdom has no layout: CodeMirror (the YAML viewer) measures text ranges once it is mounted
if (!Range.prototype.getClientRects) {
  Range.prototype.getClientRects = () => ({ length: 0, item: () => null, [Symbol.iterator]: [][Symbol.iterator] }) as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => new DOMRect();
}

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

  it("does not show a creation form just for opening the worktree section", async () => {
    setToken("t0k");
    renderAt("/node/wpe?tab=worktree");
    const open = await screen.findByRole("button", { name: "Create worktree…" });
    expect(screen.queryByPlaceholderText(/git.repo/)).toBeNull();
    fireEvent.click(open);
    expect(await screen.findByPlaceholderText(/git.repo/)).toBeTruthy();
    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls;
    expect(calls.filter(([, init]) => init?.method && init.method !== "GET")).toEqual([]);
  });

  it("combines advanced node sections under one settings tab", async () => {
    setToken("t0k");
    renderAt("/node/wpe?tab=scripts");
    await screen.findByText(/Child nodes inherit them/);
    for (const name of ["runsets", "submit", "settings"]) expect(screen.getByRole("button", { name })).toBeTruthy();
    for (const name of ["recipes", "scripts", "worktree", "notes"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
      expect(screen.getByRole("heading", { name })).toBeTruthy();
    }
    expect(screen.getByRole("button", { name: "settings" }).classList.contains("active")).toBe(true);
    await screen.findByRole("button", { name: "Create worktree…" });
    expect(document.querySelectorAll(".crumbs").length).toBe(1);
  });

  it("turns the submit presets into overrides and the CLI line", async () => {
    setToken("t0k");
    renderAt("/node/wpe?tab=submit");
    await screen.findByText("spec17 — SPEC17");
    const form = document.querySelector(".submit-form")!;
    expect(form).not.toBeNull();
    expect(screen.getByText("stages (required)", { selector: "label" })).toBeTruthy();
    const optional = screen.getByRole("group", { name: "Optional parameters" });
    for (const label of ["purpose", "priority", "binary", "points", "post steps", "overrides"]) {
      expect(within(optional).getByText(label, { selector: "label" })).toBeTruthy();
    }
    expect(form.querySelector("[title]")).toBeNull();
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
    fireEvent.click(await screen.findByText("Node details"));
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

  it("saves the comment from the collapsed node details", async () => {
    setToken("t0k");
    extra["/nodes/context"] = withNode({ comment: "old idea" });
    renderAt("/node/wpe");
    expect(screen.queryByDisplayValue("old idea")).toBeNull();
    fireEvent.click(await screen.findByText("Node details"));
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
    expect(await screen.findByText("Committed + working-tree changes vs base")).toBeTruthy();
    expect(await screen.findByText("no uncommitted changes")).toBeTruthy();
    expect(await screen.findByText(/Submodule gitlinks are excluded/)).toBeTruthy();
    expect(await screen.findByText("try a wider window")).toBeTruthy();
    fireEvent.click(screen.getByText("src/core.scala"));
    expect((await screen.findByText("+new")).className).toBe("add");
    expect(screen.getByText("-old").className).toBe("del");
    expect(writes()).toEqual([]);
  });

  it("folds the untracked files into a collapsed directory tree", async () => {
    setToken("t0k");
    extra["/nodes/context"] = withNode({}, { git: GIT });
    extra["/worktrees/status"] = { worktree: "/wt/wpe", branch: "wpe", exists: true, head: "d00d1e", dirty: 0 };
    extra["/worktrees/changes"] = {
      repos: { "": { path: "/wt/wpe", base: "main", exists: true, merge_base: "abc", ahead: 0, behind: 0, files: [],
        untracked: ["a/b/c/one.txt", "a/b/c/two.txt", "top.txt"], untracked_total: 3, untracked_truncated: false } },
      total: { files: 0, added: 0, deleted: 0, ahead: 0 },
    };
    extra["/worktrees/diff"] = { sub: "", path: "a/b/c/one.txt", diff: "+hello\n", truncated: false };
    renderAt("/node/wpe?tab=worktree");
    await screen.findByText(/untracked: 3 files in 1 dir/);
    expect(screen.queryByTestId("untracked-tree")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /untracked:/ }));
    const tree = await screen.findByTestId("untracked-tree");
    // the a/b/c single-child chain folds into one directory row
    expect(tree.textContent).toContain("a/b/c");
    expect(tree.textContent).not.toContain("one.txt");
    fireEvent.click(within(tree).getByText(/a\/b\/c/));
    expect(await screen.findByText("one.txt")).toBeTruthy();
    fireEvent.click(screen.getByText("one.txt"));
    expect(await screen.findByText("+hello")).toBeTruthy();
    const urls = (fetch as unknown as { mock: { calls: [string][] } }).mock.calls.map(([u]) => u);
    expect(urls.some((u) => u.includes("/worktrees/diff") && u.includes("path=a%2Fb%2Fc%2Fone.txt"))).toBe(true);
  });

  it("filters a task's jobs by state and keeps the filter in the URL", async () => {
    setToken("t0k");
    extra["/tasks/T1"] = {
      ...(FIXTURES["/tasks/T1"] as Record<string, unknown>),
      jobs: [
        { name: "a_1_0.5", state: "failed", host: "h1", attempt: 1, rc: 1, progress: {}, failure: { class: "design", signature: "boom" }, eta_s: 0 },
        { name: "b_1_0.5", state: "succeeded", host: "h1", attempt: 1, rc: 0, progress: {}, failure: null, eta_s: 0 },
        { name: "c_1_0.5", state: "succeeded", host: "h1", attempt: 1, rc: 0, progress: {}, failure: null, eta_s: 0 },
      ],
    };
    renderAt("/runset/wpe/overlay/20261013-1");
    const table = await screen.findByTestId("jobs-scroll");
    const inTable = (name: string) => within(table).getByText(name);
    expect(inTable("a_1_0.5")).toBeTruthy();
    // every state has a chip with its count; states without jobs are disabled
    const filter = screen.getByRole("group", { name: "filter jobs by state" });
    expect(within(filter).getByRole("button", { name: /all 3/ }).getAttribute("aria-pressed")).toBe("true");
    expect((within(filter).getByRole("button", { name: /running 0/ }) as HTMLButtonElement).disabled).toBe(true);
    const chip = [...screen.getAllByRole("button")].find((b) => b.getAttribute("aria-pressed") !== null && /succeeded/.test(b.textContent ?? ""))!;
    fireEvent.click(chip);
    expect(chip.getAttribute("aria-pressed")).toBe("true");
    await waitFor(() => expect(within(table).queryByText("a_1_0.5")).toBeNull());
    expect(inTable("b_1_0.5")).toBeTruthy();
    fireEvent.click(chip);
    expect(await within(table).findByText("a_1_0.5")).toBeTruthy();
  });

  it("shows the runset parameters with the overridden keys marked", async () => {
    setToken("t0k");
    extra["/result"] = {
      ...(FIXTURES["/result"] as Record<string, unknown>),
      params: { build: { tool: "gsim", mode: "trace" }, run: { W: 1000000, I: 2000000, points: 1090 }, post: { "pk.plugin": "perf-keys" } },
      overrides: { run: { W: 1000000 } },
      recipes: { build: { tool: "gsim" }, run: { W: 1000000 }, post: { steps: {} } },
      tasks: [
        { id: "TB", kind: "build", name: "build", state: "succeeded", counts: { succeeded: 1 }, failure: null },
        { id: "T1", kind: "run", name: "run", state: "succeeded", counts: { succeeded: 1090 }, failure: null },
      ],
    };
    extra["/tasks/TB"] = { ...(FIXTURES["/tasks/T1"] as Record<string, unknown>), id: "TB", kind: "build", jobs: [] };
    renderAt("/runset/wpe/overlay/20261013-1");
    // the run stage is selected: only its parameters are shown, under the stage card
    expect(await screen.findByText("1,000,000")).toBeTruthy();
    expect(screen.getByTestId("stage-params").textContent).toContain("run parameters");
    expect(screen.queryByText("gsim")).toBeNull();
    expect(screen.getByText("overrides: 1")).toBeTruthy();
    const overrideBadge = [...document.querySelectorAll(".badge")].find((b) => b.textContent === "set");
    expect(overrideBadge).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "full recipe (YAML)" }));
    fireEvent.click(await screen.findByRole("button", { name: "hide full recipe" }));
    // the build chip switches the card to the build parameters
    fireEvent.click(screen.getByRole("button", { name: /^build/ }));
    expect(await screen.findByText("gsim")).toBeTruthy();
    expect(screen.getByTestId("stage-params").textContent).toContain("build parameters");
    expect(screen.queryByText("1,000,000")).toBeNull();
  });

  it("groups runset outputs and details into fewer tabs", async () => {
    setToken("t0k");
    renderAt("/runset/wpe/overlay/20261013-1?tab=post");
    const results = await screen.findByRole("button", { name: "results" });
    expect(results.classList.contains("active")).toBe(true);
    for (const name of ["jobs", "code", "details"]) expect(screen.getByRole("button", { name })).toBeTruthy();
    for (const name of ["post", "files", "problem log", "provenance"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    expect(screen.getByRole("heading", { name: "post" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "files" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "details" }));
    expect(await screen.findByRole("heading", { name: "problem log" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "provenance" })).toBeTruthy();
  });

  it("shows the runset code tab and can compare against the worktree", async () => {
    setToken("t0k");
    extra["/runsets"] = [{ ...(FIXTURES["/result"] as object), ref: "wpe/overlay/20261012-2", archived: false }];
    extra["/runsets/code"] = {
      ref: "wpe/overlay/20261013-1", against: "base", legacy: false, no_code: false, drift: null, source_runset: null,
      compared_runset: null,
      record: { "": { head: "abcdef1234567890", branch: "wpe", dirty: true, shared: false, files: 2, added: 9, deleted: 1,
        patch: "super.patch", untracked: 1, omitted: [{ path: "build-vlt/", files: 51, reason: "directory of more than 50 files" }], unsaved_untracked: 0 },
        difftest: { head: "99f77ff455766d18", branch: "HEAD", dirty: false, shared: true, files: 0, added: 0, deleted: 0, patch: null, untracked: 0, omitted: [], unsaved_untracked: 0 } },
      repos: { "": { path: "/wt/wpe", base: "main", exists: true, merge_base: "abc", ahead: 1, behind: 0,
        commits: [{ sha: "d00d1e", author: "me", when: "1 hour ago", subject: "try a wider window" }],
        files: [{ status: "M", path: "src/core.scala", old_path: null, added: 3, deleted: 1, uncommitted: false }], untracked: [] } },
      total: { files: 1, added: 3, deleted: 1, ahead: 1 },
    };
    extra["/runsets/code/diff"] = { sub: "", path: "src/core.scala", diff: "+new", truncated: false };
    renderAt("/runset/wpe/overlay/20261013-1?tab=code");
    expect(await screen.findByText("try a wider window")).toBeTruthy();
    // the record: HEAD + the saved uncommitted changes; the clean submodule on one line
    const record = screen.getByTestId("code-record");
    expect(record.textContent).toContain("abcdef123456");
    expect(record.textContent).toContain("2 files +9 −1");
    expect(record.textContent).toContain("1 not saved: build-vlt/");
    expect(record.textContent).toContain("clean (1): difftest@99f77ff4");
    expect(screen.getByTestId("against-hint").textContent).toContain("what this runset adds");
    fireEvent.change(screen.getByLabelText("against"), { target: { value: "worktree" } });
    await waitFor(() => {
      const urls = (fetch as unknown as { mock: { calls: [string][] } }).mock.calls.map(([u]) => u);
      expect(urls.some((u) => u.startsWith("/runsets/code?") && u.includes("against=worktree"))).toBe(true);
    });
  });

  it("diffs a legacy runset by its recorded commits and lists them per repo", async () => {
    setToken("t0k");
    extra["/runsets"] = [];
    extra["/runsets/code"] = {
      ref: "wpe/overlay/20261013-1", against: "base", legacy: true, no_code: false, drift: null,
      source_runset: "wpe/overlay/20261012-2", compared_runset: null,
      record: { "": { head: "7a1cd62e2bbddc80", branch: "wpe", dirty: false, patch: null, untracked: 0, omitted: [], unsaved_untracked: 0 },
        difftest: { head: "99f77ff455766d18", branch: "HEAD", dirty: true, patch: "difftest.patch", files: 1, added: 3, deleted: 1,
          untracked: 0, omitted: [], unsaved_untracked: 2 } },
      repos: { "": { path: "/wt/wpe", base: "main", exists: true, merge_base: "abc", ahead: 1, behind: 0,
        commits: [{ sha: "7a1cd62", author: "me", when: "1 day ago", subject: "squash the migration" }],
        files: [{ status: "M", path: "src/core.scala", old_path: null, added: 3, deleted: 1, uncommitted: false }], untracked: [] },
        difftest: { path: "/wt/wpe/difftest", base: "", exists: true, ahead: 0, behind: 0, commits: [], files: [], untracked: [] } },
      total: { files: 1, added: 3, deleted: 1, ahead: 1 },
    };
    renderAt("/runset/wpe/overlay/20261013-1?tab=code");
    expect(await screen.findByText("squash the migration")).toBeTruthy();
    // an unchanged submodule is one entry of the "unchanged" line, not a card of its own
    expect(screen.getByTestId("unchanged-repos").textContent).toContain("difftest");
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).not.toContain("difftest ");
    expect(screen.getByTestId("legacy-note")).toBeTruthy();
    expect(screen.getByLabelText("against")).toBeTruthy();
    expect(screen.getByText(/code from runset/)).toBeTruthy();
    const repos = screen.getByTestId("code-record");
    expect(repos.textContent).toContain("recorded at submit");
    expect(repos.textContent).toContain("7a1cd62e2bbd");
    expect(repos.textContent).toContain("1 files +3 −1");
    expect(repos.textContent).toContain("2 untracked (not saved)");
  });

  it("shows only the latest task per stage in the node runsets table", async () => {
    setToken("t0k");
    const task = (id: string, kind: string, state: string) => ({ id, kind, name: kind, state, priority: 0, counts: {}, failure: null });
    const summary = {
      ...(FIXTURES["/result"] as object), ref: "wpe/20261030-1", name: "20261030-1", derived_from: "wpe/20261029-4",
      tasks: [task("B1", "build", "failed"), task("R1", "run", "cancelled"), task("B2", "build", "succeeded"), task("R2", "run", "succeeded")],
    };
    extra["/nodes/context"] = withNode({}, { runsets: [summary], runset_count: 1, params_differing: [], archived_count: 0 });
    renderAt("/node/wpe");
    const more = await screen.findByText(/\+2 earlier tasks/);
    expect(more.getAttribute("title")).toBe("build B1: failed\nrun R1: cancelled");
    expect(screen.queryByText("failed")).toBeNull();
    expect(screen.getByText("from 20261029-4")).toBeTruthy();
  });

  it("archives a runset from its page", async () => {
    setToken("t0k");
    renderAt("/runset/wpe/overlay/20261013-1");
    fireEvent.click(await screen.findByRole("button", { name: "archive" }));
    await waitFor(() => expect(writes()).toEqual([["/runsets/archive", { ref: "wpe/overlay/20261013-1", archived: true }]]));
  });

  it("archives multiple selected runsets from the node", async () => {
    setToken("t0k");
    const summaries = ["20261013-1", "20261013-2"].map((name) => ({
      ...(FIXTURES["/result"] as object), ref: `wpe/${name}`, name, archived: false,
    }));
    extra["/nodes/context"] = withNode({}, { runsets: summaries, runset_count: 2, archived_count: 0 });
    renderAt("/node/wpe");
    await screen.findByText("20261013-1");
    fireEvent.click(screen.getByLabelText("select wpe/20261013-1"));
    fireEvent.click(screen.getByLabelText("select wpe/20261013-2"));
    fireEvent.click(screen.getByRole("button", { name: "archive (2)" }));
    await waitFor(() => expect(writes()).toEqual([
      ["/runsets/archive", { ref: "wpe/20261013-1", archived: true }],
      ["/runsets/archive", { ref: "wpe/20261013-2", archived: true }],
    ]));
  });

  it("does not archive a selection with active tasks", async () => {
    setToken("t0k");
    const rs = {
      ...(FIXTURES["/result"] as object), ref: "wpe/20261013-1", name: "20261013-1", archived: false,
      tasks: [{ id: "T1", kind: "run", name: "run", state: "running", counts: {}, failure: null }],
    };
    extra["/nodes/context"] = withNode({}, { runsets: [rs], runset_count: 1, archived_count: 0 });
    renderAt("/node/wpe");
    await screen.findByText("20261013-1");
    fireEvent.click(screen.getByLabelText("select wpe/20261013-1"));
    fireEvent.click(screen.getByRole("button", { name: "archive (1)" }));
    expect(await screen.findByText("runset wpe/20261013-1 has active tasks; cancel them or wait")).toBeTruthy();
    expect(writes()).toEqual([]);
  });

  it("filters the task list by state and compares two runsets", async () => {
    setToken("t0k");
    extra["/tasks"] = [
      { id: "T1", kind: "run", node: "wpe/overlay", runset: "20261013-1", state: "succeeded", note: "", after: [],
        counts: { succeeded: 1 }, failure: null, created: "2026-10-13T10:00:00", priority: 0, blocked: false,
        design_relaunches: 0, outputs: {}, eta_s: null },
      { id: "T2", kind: "run", node: "wpe/overlay", runset: "20261013-2", state: "cancelled", note: "dependency run x failed",
        after: [], counts: {}, failure: null, created: "2026-10-13T11:00:00", priority: 0, blocked: false,
        cancelled_by: "UP", design_relaunches: 0, outputs: {}, eta_s: null },
      { id: "T3", kind: "run", node: "wpe/overlay", runset: "20261013-3", state: "queued", note: "", after: [],
        counts: { queued: 1 }, failure: null, created: "2026-10-13T12:00:00", priority: 0, blocked: true,
        design_relaunches: 0, outputs: {}, eta_s: null },
    ];
    const ctx = extra["/nodes/context"] as Record<string, unknown> | undefined;
    renderAt("/tasks");
    expect(await screen.findByText("wpe/overlay/20261013-1")).toBeTruthy();
    const chip = (label: string) =>
      [...screen.getAllByRole("button")].find((b) => b.getAttribute("aria-pressed") !== null && new RegExp(label).test(b.textContent ?? ""))!;
    fireEvent.click(chip("blocked"));
    await waitFor(() => expect(screen.queryByText("wpe/overlay/20261013-1")).toBeNull());
    expect(screen.getByText("wpe/overlay/20261013-2")).toBeTruthy();   // dependency-cancelled counts as blocked
    expect(screen.getByText("wpe/overlay/20261013-3")).toBeTruthy();   // design-blocked too
    fireEvent.click(chip("blocked"));
    expect(await screen.findByText("wpe/overlay/20261013-1")).toBeTruthy();
    void ctx;

    // two selected runsets open the code tab comparing the newer one against the older
    const summaries = ["20261013-1", "20261013-2"].map((name) => ({
      ...(FIXTURES["/result"] as object), ref: `wpe/${name}`, name,
      params: { run: { workload: "spec06", W: name === "20261013-1" ? 1000000 : 2000000 } },
      code: name === "20261013-2" ? { head: "0123456789abcdef", branch: "wpe", dirty: true, legacy: false, omitted: 0,
        uncommitted: { repos: [""], files: 2, added: 9, deleted: 1 } } : { head: "fedcba9876543210", dirty: false, legacy: false, uncommitted: null },
      differing_values: { "run.W": name === "20261013-1" ? 1000000 : 2000000, "run.dump_db": name === "20261013-1" },
    }));
    extra["/nodes/context"] = withNode({}, { runsets: summaries, runset_count: 2, params_differing: ["run.W", "run.dump_db"], archived_count: 0 });
    extra["/runsets/code"] = { ref: "wpe/20261013-2", against: "wpe/20261013-1", legacy: false, no_code: false,
      repos: {}, total: { files: 0, added: 0, deleted: 0, ahead: 0 } };
    renderAt("/node/wpe");
    await screen.findByText("20261013-1");
    // only the differing W is shown; the shared workload and the differing key without a headline
    // token are in the tooltip (the latter counted as "+1")
    const w = await screen.findByText("W=1M");
    expect(w.className).toBe("params-diff");
    expect(screen.queryByText("spec06")).toBeNull();
    const cell = w.closest(".params-cell")!;
    expect(cell.textContent).toBe("rW=1M +1");
    expect(cell.getAttribute("title")).toBe("run: spec06 · W=1M\n\nrun.dump_db = true");
    // the code column: HEAD plus a label for the uncommitted changes the build saved
    expect(screen.getByText("01234567")).toBeTruthy();
    expect(screen.getByRole("link", { name: /\+2 uncommitted/ }).getAttribute("href")).toBe("/runset/wpe/20261013-2?tab=code");
    expect(screen.queryByText(/build\.artifact/)).toBeNull();
    fireEvent.click(screen.getByLabelText("select wpe/20261013-1"));
    fireEvent.click(screen.getByLabelText("select wpe/20261013-2"));
    fireEvent.click(screen.getByRole("button", { name: "compare" }));
    await waitFor(() => {
      const urls = (fetch as unknown as { mock: { calls: [string][] } }).mock.calls.map(([u]) => u);
      expect(urls.some((u) => u.startsWith("/runsets/code?") && u.includes("against=wpe%2F20261013-1"))).toBe(true);
    });
  });
});

describe("Chinese lifecycle workflow", () => {
  beforeEach(() => { setToken("t0k"); localStorage.setItem("featx.language", "zh"); });

  it("renders settings in Chinese without translating checkpoint names or config keys", async () => {
    renderAt("/settings");
    expect(await screen.findByText("spec17_new")).toBeTruthy();
    expect(await screen.findByText("1 项正常，1 项警告，0 项失败")).toBeTruthy();
    expect(screen.getByText("git.repo")).toBeTruthy();
    expect(screen.getAllByText("设置").length).toBeGreaterThan(0);
    expect(writes()).toEqual([]);
  });

  it("recommends a hypothesis but permits creating a node without one", async () => {
    renderAt("/node/wpe");
    fireEvent.click(await screen.findByRole("button", { name: "添加子节点…" }));
    expect(screen.getByText("建议填写假设；可以现在补充，也可以在实验开始后编辑。")).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("名称（a/b 会同时创建）"), { target: { value: "new" } });
    fireEvent.click(screen.getByRole("button", { name: "创建", hidden: true }));
    await waitFor(() => expect(writes()[0]).toEqual(["/nodes", { path: "wpe/new", hypothesis: "", worktree: false, parents: true }]));
  });

  it("hides review prompts while preserving manual review APIs", async () => {
    const ref = "wpe/overlay/20261013-1";
    const rs = { ...(FIXTURES["/result"] as Record<string, unknown>), review: { needed: true, complete: true, outcome: "succeeded", ref } };
    extra["/result"] = rs;
    extra["/nodes/context"] = withNode({ path: "wpe/overlay", hypothesis: "original user hypothesis" }, { runsets: [rs], runset_count: 1 });
    renderAt(`/runset/${ref}`);
    await screen.findByText("20261013-1", { exact: false });
    expect(screen.queryByText("待评审")).toBeNull();
    expect(writes()).toEqual([]);
    cleanup();
    renderAt("/node/wpe/overlay#verdict-evidence");
    expect(await screen.findByDisplayValue("original user hypothesis")).toBeTruthy();
    expect(document.getElementById("verdict-evidence")).toBeTruthy();
    expect(screen.queryByText("待评审")).toBeNull();
    expect(writes()).toEqual([]);
    const verdict = screen.getByRole("option", { name: "支持" }).closest("select")!;
    fireEvent.change(verdict, { target: { value: "supported" } });
    await waitFor(() => expect(writes()[0]).toEqual(["/nodes/update", { ref: "wpe/overlay", verdict: "supported" }]));
    fireEvent.change(screen.getByPlaceholderText("runset 引用或 note（Ctrl+Enter 添加）"), { target: { value: ref } });
    fireEvent.click(screen.getByRole("button", { name: "添加证据" }));
    await waitFor(() => expect(writes()[1]).toEqual(["/nodes/update", { ref: "wpe/overlay", add_evidence: [ref] }]));
    extra["/events"] = [
      { seq: 1, time: "2026-10-13T10:00:00", type: "review_needed", node: "wpe/overlay", runset: "20261013-1", acked: false },
      { seq: 2, time: "2026-10-13T10:01:00", type: "runset_done", state: "succeeded", node: "wpe/overlay", runset: "20261013-1", acked: false },
    ];
    cleanup();
    renderAt("/events");
    expect(await screen.findByText("runset 完成")).toBeTruthy();
    expect(screen.queryByText("待评审")).toBeNull();
  });
});
