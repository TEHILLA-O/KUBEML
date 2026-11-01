import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { Notice, Panel, StatusMark } from "../components/ui";
import { formatLog, formatTime } from "../format";
import { useApp } from "../store";
import type { Run } from "../types";

export function LabWorkspace() {
  const role = useApp((state) => state.user?.role);
  const client = useQueryClient();
  const runs = useQuery({ queryKey: ["runs"], queryFn: () => api<{ runs: Run[] }>("/api/v1/runs"), refetchInterval: 3000 });
  const [selected, setSelected] = useState<string[]>([]);
  const [reason, setReason] = useState("Retrain candidate");
  const [active, setActive] = useState<string | null>(null);
  const comparison = useQuery({
    queryKey: ["compare", selected],
    queryFn: () => api<{ runs: Run[]; metrics: { name: string; values: Record<string, number | null>; delta_vs_first: Record<string, number> }[] }>(`/api/v1/runs/compare?ids=${selected.join(",")}`),
    enabled: selected.length >= 2,
  });
  const start = useMutation({
    mutationFn: () => api<Run>("/api/v1/training/jobs", { method: "POST", body: JSON.stringify({ reason, runtime: "local-thread" }) }),
    onSuccess: (job) => {
      setActive(job.id);
      void client.invalidateQueries({ queryKey: ["runs"] });
    },
  });
  const activeRun = runs.data?.runs.find((run) => run.id === active) ?? runs.data?.runs[0];
  const chart = (comparison.data?.metrics ?? [])
    .filter((metric) => metric.name.endsWith("roc_auc") || metric.name.endsWith("f1"))
    .slice(0, 6)
    .map((metric) => ({ name: metric.name, ...metric.values }));

  return (
    <div className="workspace two">
      <div className="grid gap-3">
        <Panel
          title="Training runs"
          action={role === "observer" || role === "approver" ? undefined : <button className="primary" type="button" onClick={() => start.mutate()} disabled={start.isPending}>Start training</button>}
        >
          <label className="field">Reason<input value={reason} onChange={(event) => setReason(event.target.value)} /></label>
          <Notice>Progress is the job state. There is no percentage because this estimator does not report one.</Notice>
          {start.error instanceof Error && <p className="text-sm text-bad">{start.error.message}</p>}
          <table className="table">
            <thead><tr><th></th><th>Status</th><th>Tracking</th><th>Validation AUC</th><th>Commit</th></tr></thead>
            <tbody>
              {(runs.data?.runs ?? []).map((run) => (
                <tr key={run.id}>
                  <td><input aria-label={`Select ${run.id}`} type="checkbox" checked={selected.includes(run.id)} onChange={() => setSelected((current) => current.includes(run.id) ? current.filter((item) => item !== run.id) : [...current, run.id])} /></td>
                  <td><StatusMark status={run.status === "succeeded" ? "healthy" : run.status === "failed" ? "failed" : "running"} /><span className="num mt-1 block text-xs">{formatTime(run.finished_at ?? run.created_at)}</span></td>
                  <td className="text-sm">{run.tracking_status}{run.tracking_error ? ` — ${run.tracking_error}` : ""}</td>
                  <td className="num">{run.metrics.validation?.roc_auc ?? "pending"}</td>
                  <td className="num text-xs">{run.git_commit || "unavailable"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
        {selected.length >= 2 && comparison.data && (
          <Panel title="Measured comparison">
            <div className="h-56">
              <ResponsiveContainer>
                <BarChart data={chart}>
                  <XAxis dataKey="name" hide />
                  <YAxis />
                  <Tooltip />
                  {comparison.data.runs.map((run) => <Bar key={run.id} dataKey={run.id} />)}
                </BarChart>
              </ResponsiveContainer>
            </div>
            <table className="table">
              <thead><tr><th>Metric</th>{comparison.data.runs.map((run) => <th key={run.id}>{run.id}</th>)}</tr></thead>
              <tbody>
                {comparison.data.metrics.filter((metric) => !metric.name.includes("confusion") && !metric.name.includes("segments")).slice(0, 12).map((metric) => (
                  <tr key={metric.name}><td>{metric.name}</td>{comparison.data?.runs.map((run) => <td key={run.id} className="num">{metric.values[run.id] ?? "—"}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </Panel>
        )}
      </div>
      <Panel title="Selected run log">
        {activeRun ? <pre className="num whitespace-pre-wrap text-xs">{formatLog(activeRun.log || "No log lines yet.")}{activeRun.failure ? `\n${activeRun.failure}` : ""}</pre> : <Notice>No run selected.</Notice>}
      </Panel>
    </div>
  );
}
