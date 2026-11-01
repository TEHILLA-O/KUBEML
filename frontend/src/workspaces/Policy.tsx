import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { Notice, Panel } from "../components/ui";
import { useApp } from "../store";

type Policy = {
  evaluation: { min_roc_auc: number; min_f1: number; max_auc_regression: number; min_samples: number };
  canary: { min_requests: number; max_error_rate: number; max_p95_latency_seconds: number };
  rollback: { deployment_timeout_seconds: number; feature_drift_auto_rollback: boolean };
  drift: { psi_threshold: number; min_samples: number };
  labels: { min_coverage: number };
  serving: { max_batch: number; decision_threshold: number };
};

export function PolicyWorkspace() {
  const role = useApp((state) => state.user?.role);
  const client = useQueryClient();
  const current = useQuery({ queryKey: ["policy"], queryFn: () => api<{ version: number; body: Policy; reason: string }>("/api/v1/policies/current") });
  const [draft, setDraft] = useState<Policy | null>(null);
  const [reason, setReason] = useState("Adjust local thresholds");
  useEffect(() => {
    if (current.data && !draft) setDraft(structuredClone(current.data.body));
  }, [current.data, draft]);
  const save = useMutation({
    mutationFn: () => api("/api/v1/policies", { method: "PUT", body: JSON.stringify({ body: draft, reason }) }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["policy"] }),
  });
  if (!draft) return <div className="workspace"><Notice>Loading the active policy.</Notice></div>;

  return (
    <div className="workspace two">
      <Panel title={`Policy v${current.data?.version ?? ""}`} action={role === "admin" ? <button className="primary" type="button" onClick={() => save.mutate()}>Save new version</button> : undefined}>
        <Notice>Saving creates a new immutable version. Feature drift stays investigation-only.</Notice>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <Threshold label="Minimum ROC AUC" value={draft.evaluation.min_roc_auc} onChange={(value) => setDraft({ ...draft, evaluation: { ...draft.evaluation, min_roc_auc: value } })} />
          <Threshold label="Minimum F1" value={draft.evaluation.min_f1} onChange={(value) => setDraft({ ...draft, evaluation: { ...draft.evaluation, min_f1: value } })} />
          <Threshold label="Canary minimum requests" value={draft.canary.min_requests} onChange={(value) => setDraft({ ...draft, canary: { ...draft.canary, min_requests: value } })} />
          <Threshold label="Maximum error rate" value={draft.canary.max_error_rate} onChange={(value) => setDraft({ ...draft, canary: { ...draft.canary, max_error_rate: value } })} />
          <Threshold label="Maximum p95 seconds" value={draft.canary.max_p95_latency_seconds} onChange={(value) => setDraft({ ...draft, canary: { ...draft.canary, max_p95_latency_seconds: value } })} />
          <Threshold label="PSI threshold" value={draft.drift.psi_threshold} onChange={(value) => setDraft({ ...draft, drift: { ...draft.drift, psi_threshold: value } })} />
          <Threshold label="Deployment timeout seconds" value={draft.rollback.deployment_timeout_seconds} onChange={(value) => setDraft({ ...draft, rollback: { ...draft.rollback, deployment_timeout_seconds: value } })} />
          <Threshold label="Minimum label coverage" value={draft.labels.min_coverage} onChange={(value) => setDraft({ ...draft, labels: { min_coverage: value } })} />
        </div>
        <label className="field mt-3">Reason<input value={reason} onChange={(event) => setReason(event.target.value)} /></label>
        {save.error instanceof Error && <p className="text-sm text-bad">{save.error.message}</p>}
        {role !== "admin" && <Notice>Only a platform administrator can publish a policy version.</Notice>}
      </Panel>
      <Panel title="Active rules">
        {policyRules(draft).map((rule) => (
          <article key={rule.title} className={`rule mb-4 ${rule.tone}`}>
            <h3 className="text-sm">{rule.title}</h3>
            <p className="mt-1 text-sm text-muted">{rule.text}</p>
          </article>
        ))}
        <details className="mt-2">
          <summary className="cursor-pointer text-sm">Policy document</summary>
          <textarea className="num mt-3 min-h-64 w-full" readOnly value={JSON.stringify(draft, null, 2)} aria-label="Policy document" />
        </details>
      </Panel>
    </div>
  );
}

function policyRules(draft: Policy): { title: string; text: string; tone: string }[] {
  return [
    { title: "Evaluation", tone: "", text: `A candidate needs validation ROC AUC of at least ${draft.evaluation.min_roc_auc} and F1 of at least ${draft.evaluation.min_f1}.` },
    { title: "Canary", tone: "", text: `Advance only after ${draft.canary.min_requests} candidate requests, with error rate at or below ${draft.canary.max_error_rate} and p95 at or below ${draft.canary.max_p95_latency_seconds} seconds.` },
    { title: "Feature drift", tone: "excluded", text: `PSI at or above ${draft.drift.psi_threshold} opens an investigation. Feature drift does not authorise an automatic rollback.` },
    { title: "Deployment", tone: "", text: `A deployment that is not ready within ${draft.rollback.deployment_timeout_seconds} seconds can use the other enabled rollback triggers.` },
    { title: "Labels", tone: "", text: `Labelled performance is reported when coverage reaches ${draft.labels.min_coverage}.` },
  ];
}

function Threshold({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) {
  return (
    <label className="field">{label}
      <input className="num" type="number" step="0.01" value={value} onChange={(event) => onChange(Number(event.target.value))} />
    </label>
  );
}
