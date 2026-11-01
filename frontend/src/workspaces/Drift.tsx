import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis } from "recharts";
import { api } from "../api";
import { Notice, Panel } from "../components/ui";
import { useApp } from "../store";
import type { DriftFeature, DriftRun } from "../types";

export function DriftWorkspace() {
  const role = useApp((state) => state.user?.role);
  const client = useQueryClient();
  const runs = useQuery({ queryKey: ["drift"], queryFn: () => api<{ runs: DriftRun[] }>("/api/v1/drift/runs") });
  const [selectedFeature, setSelectedFeature] = useState<DriftFeature | null>(null);
  const analyze = useMutation({
    mutationFn: () => api<DriftRun>("/api/v1/drift/analyze", { method: "POST" }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["drift"] });
      await client.invalidateQueries({ queryKey: ["incidents"] });
    },
  });
  const traffic = useMutation({
    mutationFn: (scenario: string) => api("/api/v1/traffic/generate", { method: "POST", body: JSON.stringify({ count: 50, scenario, label_delay_seconds: 0 }) }),
  });
  const latest = runs.data?.runs[0];
  const features = latest?.report.features ?? [];
  const chart = selectedFeature?.reference_histogram
    ? shareBins(selectedFeature.reference_histogram.counts, selectedFeature.current_histogram?.counts ?? [])
    : Object.entries(selectedFeature?.reference_share ?? {}).map(([bin, reference]) => ({
        bin,
        reference,
        current: selectedFeature?.current_share?.[bin] ?? 0,
      }));
  const prediction = latest?.report.prediction_drift ?? {};

  return (
    <div className="workspace stack gap-3">
      <Panel title="Distribution windows" action={role === "observer" || role === "approver" ? undefined : <span className="flex gap-2"><button className="quiet" type="button" onClick={() => traffic.mutate("drifted")}>Generate drifted traffic</button><button className="primary" type="button" onClick={() => analyze.mutate()}>Analyse</button></span>}>
        <Notice>{latest ? `${latest.result}. Reference ${latest.reference_rows}. Current ${latest.current_rows}. ${latest.report.note}` : "No drift analysis has been stored."}</Notice>
        {analyze.error instanceof Error && <p className="text-sm text-bad">{analyze.error.message}</p>}
        <div className="matrix mt-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))" }}>
          {features.map((feature) => (
            <button key={feature.feature} className={`cell ${feature.result}`} type="button" onClick={() => setSelectedFeature(feature)}>
              <span className="block text-sm">{feature.feature}</span>
              <span className="num text-xs">{feature.result}</span>
            </button>
          ))}
        </div>
      </Panel>
      {selectedFeature && (
        <aside className="drawer" aria-label="Drift detail">
          <button className="quiet" type="button" onClick={() => setSelectedFeature(null)}>Close</button>
          <h2 className="mt-3 text-xl">{selectedFeature.feature}</h2>
          <p className="text-sm text-muted">{selectedFeature.interpretation}</p>
          <p className="num mt-2 text-xs">n ref {selectedFeature.reference_samples} · n current {selectedFeature.current_samples}</p>
          <dl className="mt-3 text-sm">
            {Object.entries(selectedFeature.metrics).map(([key, value]) => (
              <div key={key} className="flex justify-between gap-3 border-b border-line py-1"><dt>{key}</dt><dd className="num">{metricText(value)}</dd></div>
            ))}
          </dl>
          <div className="mt-4 h-48">
            <ResponsiveContainer>
              <BarChart data={chart}>
                <XAxis dataKey="bin" />
                <Tooltip />
                <Bar dataKey="reference" fill="#8eb4ff" />
                <Bar dataKey="current" fill="#f08b82" />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <p className="mt-2 text-xs text-muted">Bars are the share of observations in each bin, so the smaller current window stays visible beside the reference.</p>
          <Notice>Prediction drift {String(prediction.result ?? "not calculated")}{prediction.samples != null ? ` · ${String(prediction.samples)} samples` : ""}{prediction.required != null ? ` · ${String(prediction.required)} required` : ""}.</Notice>
        </aside>
      )}
    </div>
  );
}

function shareBins(reference: number[], current: number[]) {
  const referenceTotal = reference.reduce((sum, count) => sum + count, 0) || 1;
  const currentTotal = current.reduce((sum, count) => sum + count, 0) || 1;
  return reference.map((count, index) => ({
    bin: index + 1,
    reference: count / referenceTotal,
    current: (current[index] ?? 0) / currentTotal,
  }));
}

function metricText(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(4);
  return String(value);
}
