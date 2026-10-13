import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
};

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = url.split("?")[0];
      const body = FIXTURES[path];
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
    await screen.findByText(/recipes reach them/);
    fireEvent.click(screen.getByRole("button", { name: "worktree" }));
    await screen.findByRole("button", { name: "Create worktree…" });
    expect(document.querySelectorAll(".crumbs").length).toBe(1);
  });
});
