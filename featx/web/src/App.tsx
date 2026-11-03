import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, getToken, onTokenChange, setToken } from "./api";
import { TreePanel } from "./components/TreePanel";
import { DocsPage } from "./pages/Docs";
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
import { I18nProvider, LanguageToggle, useI18n } from "./i18n";

function TopBar() {
  const { t } = useI18n();
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
        <NavLink to="/leaderboard">{t("Leaderboard")}</NavLink>
        <NavLink to="/tasks">{t("Tasks")}{s && s.tasks.running + s.tasks.queued > 0 ? ` (${s.tasks.running + s.tasks.queued})` : ""}</NavLink>
        <NavLink to="/events">{t("Events")}{s && s.unacked_events > 0 ? ` (${s.unacked_events})` : ""}</NavLink>
        <NavLink to="/storage">{t("Storage")}</NavLink>
        <NavLink to="/settings">{t("Settings")}</NavLink>
        <NavLink to="/docs">{t("Docs")}</NavLink>
      </nav>
      <span className="spacer" />
      {status.error && <span className="badge failed">{t("daemon unreachable")}</span>}
      {s && s.tick_errors.length > 0 && (
        <span className="badge failed" title={s.tick_errors[s.tick_errors.length - 1]}>
          {t("{count} tick errors", { count: s.tick_errors.length })}
        </span>
      )}
      {s && (
        <span className="stat">
          {t("{running} running · {queued} queued · {failed} failed", { running: s.tasks.running, queued: s.tasks.queued, failed: s.tasks.failed })}
        </span>
      )}
      {s && s.max_total_cores !== undefined && (
        <Link to="/tasks" className="stat" title={t("run cores {runUsed}/{runCap} · build cores {buildUsed}/{buildCap} (caps editable on the Tasks page)", { runUsed: s.run_cores_in_use ?? 0, runCap: s.max_total_cores || "∞", buildUsed: s.build_cores_in_use ?? 0, buildCap: s.max_build_cores || "∞" })}>
          {t("cores {used}/{cap}", { used: s.run_cores_in_use ?? 0, cap: s.max_total_cores || "∞" })}
        </Link>
      )}
      {free.length > 0 && (
        <span className="stat" title={free.map((h) => t("{host}: {cores} cores free, {jobs} featx jobs", { host: h.host, cores: h.free_cores?.toFixed(0) ?? 0, jobs: h.featx_jobs ?? 0 })).join("\n")}>
          {t("{cores} cores free / {count} hosts", { cores: free.reduce((a, h) => a + Math.max(0, h.free_cores ?? 0), 0).toFixed(0), count: free.length })}
        </span>
      )}
      {health.data && (
        <span className={`stat ${stale ? "badge failed" : ""}`} title={`pid ${health.data.pid}, started ${health.data.started}\n${health.data.root}`}>
          {health.data.host} · tick {ago(s?.last_tick, t)}
        </span>
      )}
      <button className="link" onClick={() => setToken("")}>
        {t("log out")}
      </button>
      <LanguageToggle />
    </div>
  );
}

function NotFound() {
  const { t } = useI18n();
  return <div className="muted">{t("no such page")}</div>;
}

export function App() {
  const [token, setTok] = useState(getToken());
  useEffect(() => onTokenChange(() => setTok(getToken())), []);
  return (
    <I18nProvider>
      {!token ? <LoginPage /> : (
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
                <Route path="/docs/*" element={<DocsPage />} />
                <Route path="*" element={<NotFound />} />
              </Routes>
            </div>
          </div>
        </div>
      )}
    </I18nProvider>
  );
}
