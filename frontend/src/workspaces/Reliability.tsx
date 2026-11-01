import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { Notice, Panel, StatusMark } from "../components/ui";
import { formatLatency, formatRate, formatTime } from "../format";
import { useApp } from "../store";
import type { Incident, Release } from "../types";

type Point = { time: string; at?: string; requests: number; error_rate: number; p95_ms: number | null };
type Faults = { latency_ms: number; error_rate: number; invalid_output: boolean; not_ready: boolean; not_live: boolean };
type ClusterReady = { connected: boolean; reason: string; pods: { ready: boolean }[] };

export function ReliabilityWorkspace() {
  const role = useApp((state) => state.user?.role);
  const client = useQueryClient();
  const series = useQuery({ queryKey: ["series"], queryFn: () => api<{ source: string; last_measurement_at: string | null; points: Point[] }>("/api/v1/metrics/series?minutes=120") });
  const incidents = useQuery({ queryKey: ["incidents"], queryFn: () => api<{ incidents: Incident[] }>("/api/v1/incidents") });
  const releases = useQuery({ queryKey: ["releases"], queryFn: () => api<{ releases: Release[] }>("/api/v1/releases") });
  const cluster = useQuery({ queryKey: ["cluster"], queryFn: () => api<ClusterReady>("/api/v1/cluster") });
  const performance = useQuery({ queryKey: ["performance"], queryFn: () => api<{ available: boolean; reason?: string; coverage?: number; accuracy?: number; f1?: number; labelled?: number; predictions?: number }>("/api/v1/performance") });
  const [selected, setSelected] = useState<string | null>(null);
  const incident = incidents.data?.incidents.find((item) => item.id === selected) ?? incidents.data?.incidents[0];
  const ingest = useMutation({
    mutationFn: () => api("/api/v1/labels/ingest", { method: "POST", body: JSON.stringify({ min_age_seconds: 0 }) }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["performance"] }),
  });
  const points = series.data?.points ?? [];
  const chart = points.map((point) => ({ ...point, error_pct: point.error_rate * 100 }));
  const replay = useMemo(
    () => buildReplay(points, incident, releases.data?.releases ?? [], cluster.data),
    [points, incident, releases.data, cluster.data],
  );

  return (
    <div className="workspace two">
      <div className="grid gap-3">
        <Panel title={`Service measurements · ${series.data?.source ?? "operational database"}`}>
          {points.length === 0 ? <Notice>{emptyWindow(series.data?.last_measurement_at)}</Notice> : (
            <div className="h-56">
              <ResponsiveContainer>
                <LineChart data={chart}>
                  <XAxis dataKey="time" />
                  <YAxis yAxisId="latency" />
                  <YAxis yAxisId="errors" orientation="right" />
                  <Tooltip />
                  <Line yAxisId="latency" type="monotone" dataKey="p95_ms" name="p95 ms" stroke="#8eb4ff" dot={false} />
                  <Line yAxisId="errors" type="monotone" dataKey="error_pct" name="error %" stroke="#f08b82" dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
          <p className="num mt-2 text-xs">{points.length ? `Latest p95 ${formatLatency(points.at(-1)?.p95_ms)} · error ${formatRate(points.at(-1)?.error_rate)}` : ""}</p>
          <p className="num text-xs text-muted">{series.data?.last_measurement_at ? `Last measurement ${formatTime(series.data.last_measurement_at)}` : "No measurement has been stored."}</p>
        </Panel>
        <Panel title="Investigation clock">
          {replay.length === 0 ? <Notice>No incident, release event, or measurement is available to place on the clock.</Notice> : (
            <ol className="mt-1">
              {replay.map((item) => (
                <li key={`${item.at}-${item.label}`} className="border-b border-line py-2 text-sm">
                  <span className="num text-xs">{formatTime(item.at)}</span>
                  <span className="ml-2">{item.label}</span>
                  <span className="mt-1 block text-xs text-muted">{item.detail}</span>
                </li>
              ))}
            </ol>
          )}
        </Panel>
        <Panel title="Labelled performance" action={role === "observer" ? undefined : <button className="quiet" type="button" onClick={() => ingest.mutate()}>Ingest due labels</button>}>
          {performance.data?.available ? (
            <p className="num">Accuracy {metric(performance.data.accuracy)} · F1 {metric(performance.data.f1)} · coverage {formatRate(performance.data.coverage)} · labels {performance.data.labelled}/{performance.data.predictions}</p>
          ) : <Notice>{performance.data?.reason ?? "Performance is unavailable until delayed labels are ingested."}</Notice>}
        </Panel>
        <FaultControls role={role} />
      </div>
      <div className="grid content-start gap-3">
        <Panel title="Incidents">
          {(incidents.data?.incidents ?? []).map((item) => (
            <button key={item.id} className="block w-full border-b border-line py-2 text-left" type="button" onClick={() => setSelected(item.id)}>
              <StatusMark status={item.status === "resolved" ? "healthy" : item.status === "unresolved" ? "failed" : "degraded"} />
              <span className="ml-2">{item.trigger}</span>
              <span className="mt-1 block text-xs text-muted">{item.summary}</span>
            </button>
          ))}
          {incidents.data?.incidents.length === 0 && <Notice>No incidents recorded.</Notice>}
        </Panel>
        {incident && (
          <Panel title="Investigation">
            <p className="text-sm">{incident.summary}</p>
            <p className="num mt-2 text-xs">Opened {formatTime(incident.opened_at)} · {incident.resolved_at ? `resolved ${formatTime(incident.resolved_at)}` : "still open"}</p>
            <ul className="mt-3 text-sm">
              {incident.actions.map((action, index) => <li key={`${action.action}-${index}`}>{action.action}{action.passed === undefined ? "" : action.passed ? " verified" : " failed"}</li>)}
            </ul>
            <Evidence evidence={incident.evidence} />
          </Panel>
        )}
      </div>
    </div>
  );
}

function FaultControls({ role }: { role: string | undefined }) {
  const client = useQueryClient();
  const faults = useQuery({ queryKey: ["faults"], queryFn: () => api<Faults>("/api/v1/faults") });
  const [latency, setLatency] = useState(0);
  const [errorRate, setErrorRate] = useState(0);
  const [invalid, setInvalid] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [armed, setArmed] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    if (!faults.data || loaded) return;
    setLatency(faults.data.latency_ms);
    setErrorRate(faults.data.error_rate);
    setInvalid(faults.data.invalid_output);
    setLoaded(true);
  }, [faults.data, loaded]);
  const apply = useMutation({
    mutationFn: (body: Faults) => api<Faults>("/api/v1/faults", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: (next) => {
      setLatency(next.latency_ms);
      setErrorRate(next.error_rate);
      setInvalid(next.invalid_output);
      setLoaded(true);
      setArmed(false);
      setMessage("The local router accepted the fault change.");
      void client.invalidateQueries({ queryKey: ["faults"] });
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : "Fault change failed."),
  });
  if (role !== "admin") return <Panel title="Fault injection"><Notice>Only a platform administrator can inject faults into the local router.</Notice></Panel>;
  const draft: Faults = {
    latency_ms: latency,
    error_rate: errorRate,
    invalid_output: invalid,
    not_ready: false,
    not_live: false,
  };
  const harmful = draft.latency_ms > 0 || draft.error_rate > 0 || draft.invalid_output;
  return (
    <Panel title="Fault injection">
      <Notice>These faults change the local router. They are real serving behaviour, limited to this process.</Notice>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <label className="field">Latency milliseconds<input className="num" type="number" min={0} max={5000} value={latency} onChange={(event) => setLatency(Number(event.target.value))} /></label>
        <label className="field">Error rate<input className="num" type="number" min={0} max={1} step="0.05" value={errorRate} onChange={(event) => setErrorRate(Number(event.target.value))} /></label>
      </div>
      <label className="mt-2 flex items-center gap-2 text-sm"><input type="checkbox" checked={invalid} onChange={(event) => setInvalid(event.target.checked)} /> Invalid inference output</label>
      <label className="mt-2 flex items-center gap-2 text-sm"><input type="checkbox" checked={armed} onChange={(event) => setArmed(event.target.checked)} /> I am changing live local-router behaviour</label>
      <div className="mt-3 flex gap-2">
        <button className="danger" type="button" disabled={!armed || !harmful || apply.isPending} onClick={() => apply.mutate({ ...draft, not_ready: false, not_live: false })}>Apply fault</button>
        <button className="quiet" type="button" disabled={apply.isPending} onClick={() => apply.mutate({ latency_ms: 0, error_rate: 0, invalid_output: false, not_ready: false, not_live: false })}>Restore healthy serving</button>
      </div>
      {message && <p className="mt-2 text-sm">{message}</p>}
    </Panel>
  );
}

function emptyWindow(last: string | null | undefined): string {
  if (!last) return "No measurements are stored.";
  return `No measurements in this window. Last measurement ${formatTime(last)}.`;
}

function buildReplay(points: Point[], incident: Incident | undefined, releases: Release[], cluster: ClusterReady | undefined) {
  const marks: { at: string; label: string; detail: string }[] = [];
  for (const point of points) {
    if (!point.at) continue;
    marks.push({ at: point.at, label: "Measurement", detail: `${point.requests} requests · error ${formatRate(point.error_rate)} · p95 ${formatLatency(point.p95_ms)}` });
  }
  if (incident?.opened_at) {
    marks.push({ at: incident.opened_at, label: "Incident opened", detail: `${incident.trigger}. ${incident.summary}` });
    const authorised = incident.evidence.rollback_authorised;
    if (typeof authorised === "boolean") {
      marks.push({
        at: incident.opened_at,
        label: "Rollback decision",
        detail: authorised ? "Policy authorised a rollback." : "Policy did not authorise a rollback.",
      });
    }
  }
  for (const release of releases) {
    for (const event of release.events) {
      if (!event.at) continue;
      marks.push({ at: event.at, label: `${event.from} → ${event.to}`, detail: event.reason });
    }
  }
  if (cluster) {
    const ready = cluster.pods.filter((pod) => pod.ready).length;
    marks.push({
      at: new Date().toISOString(),
      label: "Readiness now",
      detail: cluster.connected ? `${ready}/${cluster.pods.length} pods ready. This is the current probe, not a historical sample.` : cluster.reason || "Cluster readiness was not observed.",
    });
  }
  return marks.sort((left, right) => left.at.localeCompare(right.at));
}

function metric(value: number | undefined): string {
  return value == null ? "—" : value.toFixed(3);
}

function Evidence({ evidence }: { evidence: Record<string, unknown> }) {
  const features = Array.isArray(evidence.features) ? evidence.features.map(String) : [];
  const rows = Object.entries(evidence).filter(([key]) => key !== "features");
  return (
    <div className="mt-3 text-sm">
      {rows.map(([key, value]) => (
        <p key={key} className="flex justify-between gap-3 border-b border-line py-1">
          <span>{key.replaceAll("_", " ")}</span>
          <span className="num text-right">{typeof value === "boolean" ? (value ? "yes" : "no") : String(value)}</span>
        </p>
      ))}
      {features.length > 0 && <p className="mt-2 text-muted">Features: {features.join(", ")}</p>}
    </div>
  );
}
