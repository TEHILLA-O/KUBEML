import { useEffect } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { formatLatency, formatRate, formatTime, shortHash } from "../format";
import { useApp } from "../store";
import type { Summary } from "../types";
import { CommandPalette } from "./CommandPalette";
import { StatusMark } from "./ui";

function windowLabel(summary: Summary): string {
  const current = `${summary.requests_5m} req / 5m · err ${formatRate(summary.error_rate_5m)} · p95 ${formatLatency(summary.p95_ms_5m)}`;
  if (summary.requests_5m > 0) return current;
  if (!summary.last_measurement_at) return `${current} · no measurements recorded`;
  return `${current} · last ${formatTime(summary.last_measurement_at)}`;
}

const links = [
  ["/", "Mission"],
  ["/lab", "Model lab"],
  ["/registry", "Registry"],
  ["/releases", "Release studio"],
  ["/drift", "Drift"],
  ["/reliability", "Reliability"],
  ["/cluster", "Cluster"],
  ["/timeline", "Timeline"],
  ["/policy", "Policy"],
];

export function Shell() {
  const user = useApp((state) => state.user);
  const theme = useApp((state) => state.theme);
  const setTheme = useApp((state) => state.setTheme);
  const setPalette = useApp((state) => state.setPalette);
  const signOut = useApp((state) => state.signOut);
  const summary = useQuery({ queryKey: ["summary"], queryFn: () => api<Summary>("/api/v1/summary"), refetchInterval: 5000 });

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPalette(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setPalette, theme]);

  return (
    <div className="app-shell">
      <header className="ribbon">
        <div className="brand">
          <svg className="mark" viewBox="0 0 32 32" aria-hidden="true">
            <circle cx="16" cy="16" r="4" fill="currentColor" />
            <ellipse cx="16" cy="16" rx="13" ry="6" fill="none" stroke="currentColor" />
            <ellipse cx="16" cy="16" rx="6" ry="13" fill="none" stroke="currentColor" />
          </svg>
          KUBEML
        </div>
        <span className="chip"><span className="kicker">Runtime </span><span className="num">{summary.data?.runtime ?? "connecting"}</span></span>
        <span className="chip"><span className="kicker">Model </span><span className="num">{summary.data?.model ? `${summary.data.model.name} v${summary.data.model.version}` : "none"}</span></span>
        <span className="chip"><span className="kicker">Release </span><span className="num">{summary.data?.release ? `${summary.data.release.status}` : "none"}</span></span>
        <span className="ml-auto flex items-center gap-2">
          <StatusMark status={summary.data?.cluster_connected ? "healthy" : "unavailable"} />
          <span className="num whitespace-nowrap text-xs text-muted">{summary.data ? windowLabel(summary.data) : ""}</span>
          <button className="quiet" type="button" onClick={() => setPalette(true)}>Commands <span className="num text-xs">Ctrl K</span></button>
          <button className="quiet" type="button" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>{theme === "dark" ? "Light" : "Dark"}</button>
          <span className="whitespace-nowrap text-sm">{user?.name} <span className="text-xs text-muted">{user?.role}</span></span>
          <button className="quiet" type="button" onClick={signOut}>Sign out</button>
        </span>
      </header>
      <nav className="workspaces" aria-label="Workspaces">
        {links.map(([path, label]) => (
          <NavLink key={path} to={path} end={path === "/"} className="workspace-tab">{label}</NavLink>
        ))}
      </nav>
      <Outlet />
      <CommandPalette />
      <span className="sr-only">{shortHash(summary.data?.model?.sha256)}</span>
    </div>
  );
}
