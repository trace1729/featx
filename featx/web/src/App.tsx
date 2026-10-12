import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, getToken, onTokenChange, setToken } from "./api";
import { TreePanel } from "./components/TreePanel";
import { EventsPage } from "./pages/Events";
import { LeaderboardPage } from "./pages/Leaderboard";
import { LoginPage } from "./pages/Login";
import { NodePage } from "./pages/Node";
import { RunsetPage } from "./pages/Runset";
import { SettingsPage } from "./pages/Settings";
import { StoragePage } from "./pages/Storage";
import { TasksPage } from "./pages/Tasks";
import type { Health, HostRow, Status } from "./types";
import { ago } from "./util";

function TopBar() {
  const health = useQuery({ queryKey: ["health"], queryFn: () => api.get<Health>("/health"), refetchInterval: 10000 });
  const status = useQuery({ queryKey: ["status"], queryFn: () => api.get<Status>("/status"), refetchInterval: 5000 });
  const hosts = useQuery({ queryKey: ["hosts"], queryFn: () => api.get<HostRow[]>("/hosts"), refetchInterval: 60000 });
  const s = status.data;
  const free = (hosts.data ?? []).filter((h) => h.free_cores !== undefined);
  const stale = s ? Date.now() / 1000 - s.last_tick > 60 : false;
  return (
    <div className="topbar">
      <span className="brand">featx</span>
      <nav>
        <NavLink to="/leaderboard">Leaderboard</NavLink>
        <NavLink to="/tasks">Tasks{s && s.tasks.running + s.tasks.queued > 0 ? ` (${s.tasks.running + s.tasks.queued})` : ""}</NavLink>
        <NavLink to="/events">Events{s && s.unacked_events > 0 ? ` (${s.unacked_events})` : ""}</NavLink>
        <NavLink to="/storage">Storage</NavLink>
        <NavLink to="/settings">Settings</NavLink>
      </nav>
      <span className="spacer" />
      {status.error && <span className="badge failed">daemon unreachable</span>}
      {s && s.tick_errors.length > 0 && (
        <span className="badge failed" title={s.tick_errors[s.tick_errors.length - 1]}>
          {s.tick_errors.length} tick errors
        </span>
      )}
      {s && (
        <span className="stat">
          {s.tasks.running} running · {s.tasks.queued} queued · {s.tasks.failed} failed
        </span>
      )}
      {free.length > 0 && (
        <span className="stat" title={free.map((h) => `${h.host}: ${h.free_cores?.toFixed(0)} cores free, ${h.featx_jobs} featx jobs`).join("\n")}>
          {free.reduce((a, h) => a + Math.max(0, h.free_cores ?? 0), 0).toFixed(0)} cores free / {free.length} hosts
        </span>
      )}
      {health.data && (
        <span className={`stat ${stale ? "badge failed" : ""}`} title={`pid ${health.data.pid}, started ${health.data.started}\n${health.data.root}`}>
          {health.data.host} · tick {ago(s?.last_tick)}
        </span>
      )}
      <button className="link" onClick={() => setToken("")}>
        log out
      </button>
    </div>
  );
}

export function App() {
  const [token, setTok] = useState(getToken());
  useEffect(() => onTokenChange(() => setTok(getToken())), []);
  if (!token) return <LoginPage />;
  return (
    <div className="app">
      <TopBar />
      <div className="body">
        <div className="sidebar">
          <TreePanel />
        </div>
        <div className="main">
          <Routes>
            <Route path="/" element={<Navigate to="/leaderboard" replace />} />
            <Route path="/leaderboard" element={<LeaderboardPage />} />
            <Route path="/node/*" element={<NodePage />} />
            <Route path="/runset/*" element={<RunsetPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/events" element={<EventsPage />} />
            <Route path="/storage" element={<StoragePage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<div className="muted">no such page</div>} />
          </Routes>
        </div>
      </div>
    </div>
  );
}
