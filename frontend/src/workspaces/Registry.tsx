import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Background, ReactFlow } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { api } from "../api";
import { Notice, Panel, StatusMark } from "../components/ui";
import { shortHash } from "../format";
import type { Version } from "../types";

type Lineage = {
  version: Version;
  training_run: { id: string; mlflow_run_id: string } | null;
  dataset: { fingerprint: string; rows: number; schema_version: string };
  git_commit: string;
  image_digest: string;
  releases: { id: string; status: string }[];
};

export function RegistryWorkspace() {
  const versions = useQuery({ queryKey: ["models"], queryFn: () => api<{ versions: Version[] }>("/api/v1/models") });
  const [selected, setSelected] = useState<string | null>(null);
  const currentId = selected ?? versions.data?.versions[0]?.id ?? "";
  const lineage = useQuery({ queryKey: ["lineage", currentId], queryFn: () => api<Lineage>(`/api/v1/versions/${currentId}/lineage`), enabled: Boolean(currentId) });
  const detail = useQuery({ queryKey: ["version", currentId], queryFn: () => api<{ version: Version; gates: { gate: string; passed: boolean; summary: string }[] }>(`/api/v1/versions/${currentId}`), enabled: Boolean(currentId) });
  const nodeStyle = {
    background: "var(--panel)",
    color: "var(--ink)",
    border: "1px solid var(--line)",
    borderRadius: 12,
    fontSize: 12,
    width: 168,
  };
  const graph = lineage.data
    ? [
        { id: "version", position: { x: 0, y: 40 }, data: { label: `v${lineage.data.version.version} ${lineage.data.version.alias}` }, style: nodeStyle },
        { id: "run", position: { x: 240, y: 40 }, data: { label: lineage.data.training_run?.id ?? "run unavailable" }, style: nodeStyle },
        { id: "data", position: { x: 480, y: 0 }, data: { label: shortHash(lineage.data.dataset.fingerprint) }, style: nodeStyle },
        { id: "commit", position: { x: 480, y: 120 }, data: { label: lineage.data.git_commit || "commit unavailable" }, style: nodeStyle },
      ]
    : [];

  return (
    <div className="workspace two">
      <Panel title="Model catalogue">
        <div className="grid gap-2">
          {(versions.data?.versions ?? []).map((version) => (
            <button key={version.id} className="panel-block text-left" type="button" onClick={() => setSelected(version.id)}>
              <div className="flex items-center justify-between">
                <strong>{version.name} v{version.version}</strong>
                <StatusMark status={version.alias === "champion" ? "healthy" : version.alias === "rejected" ? "failed" : "idle"} />
              </div>
              <p className="mt-1 text-sm text-muted">{version.task} · {version.framework} · {version.alias}</p>
              <p className="num mt-2 text-xs">AUC {version.metrics.validation?.roc_auc ?? "—"} · {version.artifact_bytes} bytes · {shortHash(version.artifact_sha256)}</p>
            </button>
          ))}
        </div>
      </Panel>
      <div className="grid gap-3">
        <Panel title="Lineage">
          <div className="h-64">
            <ReactFlow nodes={graph} edges={[{ id: "a", source: "version", target: "run" }, { id: "b", source: "run", target: "data" }, { id: "c", source: "run", target: "commit" }]} fitView proOptions={{ hideAttribution: true }}>
              <Background />
            </ReactFlow>
          </div>
          {lineage.data && <Notice>Image digest {lineage.data.image_digest || "unavailable"}. Schema {lineage.data.dataset.schema_version}. Rows {lineage.data.dataset.rows}.</Notice>}
        </Panel>
        <Panel title="Gates">
          {(detail.data?.gates ?? []).map((gate) => (
            <p key={gate.gate} className="flex items-center justify-between border-b border-line py-2 text-sm">
              <span>{gate.gate}</span>
              <StatusMark status={gate.passed ? "healthy" : "failed"} />
            </p>
          ))}
          {detail.data && detail.data.gates.length === 0 && <Notice>No gate evidence stored for this version.</Notice>}
        </Panel>
      </div>
    </div>
  );
}
