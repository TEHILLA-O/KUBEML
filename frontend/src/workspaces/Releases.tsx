import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { Notice, Panel, StatusMark } from "../components/ui";
import { formatLatency, formatRate, formatTime } from "../format";
import { useApp } from "../store";
import type { Release, Version } from "../types";

const stages = ["build", "validate", "package", "stage", "canary", "verify", "promote"];

export function ReleaseWorkspace() {
  const role = useApp((state) => state.user?.role);
  const client = useQueryClient();
  const releases = useQuery({ queryKey: ["releases"], queryFn: () => api<{ releases: Release[] }>("/api/v1/releases"), refetchInterval: 4000 });
  const versions = useQuery({ queryKey: ["models"], queryFn: () => api<{ versions: Version[] }>("/api/v1/models") });
  const [releaseId, setReleaseId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState("");
  const [message, setMessage] = useState("");
  const current = releases.data?.releases.find((item) => item.id === releaseId) ?? releases.data?.releases[0];
  const stable = releases.data?.releases.find((item) => item.status === "promoted");
  const eligible = versions.data?.versions.find((item) => item.approval === "eligible");

  const call = useMutation({
    mutationFn: async (action: string) => {
      if (!current && action !== "create") throw new Error("No release selected.");
      if (action === "create") {
        if (!eligible) throw new Error("No eligible candidate version.");
        return api<Release>("/api/v1/releases", { method: "POST", body: JSON.stringify({ model_version_id: eligible.id, environment: "local" }) });
      }
      if (action === "rollback") {
        return api(`/api/v1/releases/${current?.id}/rollback`, {
          method: "POST",
          headers: { "Idempotency-Key": `ui-${current?.id}-${Date.now()}` },
          body: JSON.stringify({ reason: "Operator rollback from release studio.", trigger: "manual" }),
        });
      }
      const path = action === "canary" ? `/api/v1/releases/${current?.id}/canary` : `/api/v1/releases/${current?.id}/${action}`;
      const body = action === "canary" ? { weight: 5 } : { reason: "Operator action from release studio." };
      return api(path, { method: "POST", body: JSON.stringify(body) });
    },
    onSuccess: () => {
      setMessage("The control plane accepted the action.");
      void client.invalidateQueries({ queryKey: ["releases"] });
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : "Action failed."),
  });

  return (
    <div className="workspace stack gap-3">
      <Panel title="Release track" action={<button className="primary" type="button" disabled={role === "observer"} onClick={() => call.mutate("create")}>Create local release</button>}>
        {current ? (
          <div className="track">
            {stages.map((stage) => (
              <article key={stage} className={current.stages[stage] ?? "pending"}>
                <p className="kicker">{stage}</p>
                <StatusMark status={current.stages[stage] ?? "pending"} />
              </article>
            ))}
          </div>
        ) : <Notice>No release has been recorded.</Notice>}
        <p className="mt-3 text-sm text-muted">{message}</p>
      </Panel>
      <div className="compare">
        <Slot title="Stable" release={stable} />
        <Slot title="Candidate" release={current && current.status !== "promoted" ? current : undefined} />
      </div>
      <Panel title="Actions">
        <div className="mb-3 flex flex-wrap gap-2">
          {(releases.data?.releases ?? []).map((release) => (
            <button key={release.id} className="quiet" type="button" onClick={() => setReleaseId(release.id)}>{release.id} · {release.status}</button>
          ))}
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="quiet" type="button" disabled={role === "observer"} onClick={() => call.mutate("deploy")}>Deploy candidate</button>
          <button className="quiet" type="button" disabled={role === "observer"} onClick={() => call.mutate("canary")}>Set 5% canary</button>
          <button className="quiet" type="button" disabled={role !== "admin" && role !== "approver"} onClick={() => call.mutate("approve")}>Record approval</button>
          <button className="quiet" type="button" disabled={role !== "admin" && role !== "approver"} onClick={() => call.mutate("promote")}>Promote</button>
        </div>
        <label className="field mt-3">Type the release id to roll back<input value={confirm} onChange={(event) => setConfirm(event.target.value)} /></label>
        <button className="danger" type="button" disabled={confirm !== current?.id || (role !== "admin" && role !== "approver")} onClick={() => call.mutate("rollback")}>Roll back</button>
        {current && <p className="num mt-2 text-xs">Configured weight {current.canary_weight}. Measured candidate requests {current.routing.candidate?.requests ?? 0}. Stable requests {current.routing.stable?.requests ?? 0}.</p>}
      </Panel>
      <Panel title="Events">
        {(current?.events ?? []).map((event) => (
          <p key={event.id} className="border-b border-line py-2 text-sm"><span className="num">{formatTime(event.at)}</span> {event.actor}: {event.reason}</p>
        ))}
      </Panel>
    </div>
  );
}

function Slot({ title, release }: { title: string; release?: Release }) {
  return (
    <Panel title={title}>
      {release ? (
        <dl className="text-sm">
          <Row label="Version" value={String(release.model_version ?? "—")} />
          <Row label="Status" value={release.status} />
          <Row label="Error rate" value={formatRate(release.routing.candidate?.error_rate ?? release.routing.stable?.error_rate)} />
          <Row label="p95" value={formatLatency(release.routing.candidate?.p95_ms ?? release.routing.stable?.p95_ms)} />
          <Row label="Digest" value={release.image_digest || "unavailable"} />
          <Row label="Artifact" value={release.artifact_sha256.slice(0, 16)} />
        </dl>
      ) : <Notice>Nothing is assigned to this slot.</Notice>}
    </Panel>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return <div className="flex justify-between gap-3 border-b border-line py-2"><dt className="text-muted">{label}</dt><dd className="num">{value}</dd></div>;
}
