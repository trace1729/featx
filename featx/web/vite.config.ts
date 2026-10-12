/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// featxd serves the built bundle at /ui/ and its REST API at the origin root.
// `npm run dev` proxies the API to a daemon (FEATX_DEV_API, default the dev daemon on :8766).
const api = process.env.FEATX_DEV_API ?? "http://127.0.0.1:8766";
const apiPrefixes = [
  "health", "status", "hosts", "tree", "nodes", "runsets", "result", "recipes", "scripts", "worktrees",
  "tasks", "eta", "events", "problem-log", "storage", "gc", "baselines", "bins", "leaderboard", "config",
  "files",
];

export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 1500 },
  server: {
    proxy: Object.fromEntries(apiPrefixes.map((p) => [`^/${p}(/|\\?|$)`, { target: api, changeOrigin: true }])),
  },
  test: { environment: "jsdom", globals: true },
});
