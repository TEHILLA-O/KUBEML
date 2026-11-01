import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { useApp } from "../store";
import type { Incident, Release, Version } from "../types";

const pages = [
  ["Mission control", "/"],
  ["Model lab", "/lab"],
  ["Registry", "/registry"],
  ["Release studio", "/releases"],
  ["Drift observatory", "/drift"],
  ["Reliability", "/reliability"],
  ["Cluster", "/cluster"],
  ["Timeline", "/timeline"],
  ["Policy lab", "/policy"],
];

export function CommandPalette() {
  const open = useApp((state) => state.palette);
  const setPalette = useApp((state) => state.setPalette);
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const versions = useQuery({ queryKey: ["models"], queryFn: () => api<{ versions: Version[] }>("/api/v1/models"), enabled: open });
  const releases = useQuery({ queryKey: ["releases"], queryFn: () => api<{ releases: Release[] }>("/api/v1/releases"), enabled: open });
  const incidents = useQuery({ queryKey: ["incidents"], queryFn: () => api<{ incidents: Incident[] }>("/api/v1/incidents"), enabled: open });
  const commands = useMemo(() => {
    const items = pages.map(([label, path]) => ({ id: path, label, hint: "Workspace", run: () => navigate(path) }));
    for (const version of versions.data?.versions ?? []) {
      items.push({ id: version.id, label: `${version.name} v${version.version}`, hint: version.alias, run: () => navigate("/registry") });
    }
    for (const release of releases.data?.releases ?? []) {
      items.push({ id: release.id, label: release.id, hint: release.status, run: () => navigate("/releases") });
    }
    for (const incident of incidents.data?.incidents ?? []) {
      items.push({ id: incident.id, label: incident.trigger, hint: incident.status, run: () => navigate("/reliability") });
    }
    items.push({ id: "grafana", label: "Open Grafana", hint: "Observability", run: () => window.open("http://127.0.0.1:3000", "_blank", "noopener") });
    const needle = query.trim().toLowerCase();
    return items.filter((item) => `${item.label} ${item.hint}`.toLowerCase().includes(needle));
  }, [incidents.data, navigate, query, releases.data, versions.data]);

  useEffect(() => {
    if (!open) setQuery("");
  }, [open]);

  if (!open) return null;
  return (
    <div className="palette" role="presentation" onClick={() => setPalette(false)}>
      <form role="dialog" aria-label="Command palette" onClick={(event) => event.stopPropagation()}>
        <input autoFocus placeholder="Search models, releases, incidents, workspaces" value={query} onChange={(event) => setQuery(event.target.value)} aria-label="Command search" />
        <div className="max-h-80 overflow-auto pb-2">
          {commands.map((command) => (
            <button
              key={command.id + command.label}
              type="button"
              onClick={() => {
                command.run();
                setPalette(false);
              }}
            >
              <span>{command.label}</span>
              <span className="float-right text-xs text-muted">{command.hint}</span>
            </button>
          ))}
          {commands.length === 0 && <p className="px-4 py-3 text-sm text-muted">No matching command.</p>}
        </div>
      </form>
    </div>
  );
}
