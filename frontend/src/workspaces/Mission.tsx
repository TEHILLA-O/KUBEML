import { useQuery } from "@tanstack/react-query";
import { Background, Controls, Handle, Position, ReactFlow, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { api } from "../api";
import { StatusMark } from "../components/ui";
import { formatTime } from "../format";
import { useApp } from "../store";
import type { Topology, TopologyNode } from "../types";

const positions: Record<string, { x: number; y: number }> = {
  dataset: { x: 20, y: 170 },
  training: { x: 280, y: 30 },
  registry: { x: 540, y: 170 },
  validation: { x: 800, y: 30 },
  container: { x: 1060, y: 170 },
  kubernetes: { x: 800, y: 330 },
  inference: { x: 540, y: 430 },
  monitoring: { x: 280, y: 330 },
  reliability: { x: 20, y: 430 },
};

function StageNode({ data, selected }: NodeProps) {
  const node = data as unknown as TopologyNode;
  return (
    <article className={`stage ${node.status} ${selected ? "selected" : ""}`}>
      <Handle type="target" position={Position.Left} />
      <p className="kicker">{node.kind}</p>
      <h3 className="mt-1 text-base">{node.label}</h3>
      <StatusMark status={node.status} />
      <p className="mt-2 line-clamp-3 text-xs text-muted">{node.summary}</p>
      <Handle type="source" position={Position.Right} />
    </article>
  );
}

const nodeTypes = { stage: StageNode };
const edges = [
  ["dataset", "training"],
  ["training", "registry"],
  ["registry", "validation"],
  ["validation", "container"],
  ["container", "kubernetes"],
  ["kubernetes", "inference"],
  ["inference", "monitoring"],
  ["monitoring", "reliability"],
].map(([source, target]) => ({ id: `${source}-${target}`, source, target, style: { stroke: "var(--line)" } }));

export function MissionWorkspace() {
  const selected = useApp((state) => state.selectedNode);
  const setSelected = useApp((state) => state.setSelectedNode);
  const topology = useQuery({ queryKey: ["topology"], queryFn: () => api<Topology>("/api/v1/topology"), refetchInterval: 5000 });
  const timeline = useQuery({ queryKey: ["timeline"], queryFn: () => api<{ events: { id: string; kind: string; title: string; detail: string; at: string | null; status: string }[] }>("/api/v1/timeline") });
  const nodes: Node[] = (topology.data?.nodes ?? []).map((node) => ({
    id: node.id,
    type: "stage",
    position: positions[node.id] ?? { x: 0, y: 0 },
    data: node,
    selected: node.id === selected,
  }));
  const current = topology.data?.nodes.find((node) => node.id === selected) ?? topology.data?.nodes[0];

  return (
    <section className="mission" aria-label="Mission control">
      <div className="canvas">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          onNodeClick={(_event, node) => setSelected(node.id)}
          proOptions={{ hideAttribution: true }}
        >
          <Background color="var(--line)" gap={32} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <aside className="inspector" aria-live="polite">
        <p className="kicker">Inspector</p>
        {current ? (
          <>
            <h1 className="mt-2 text-2xl">{current.label}</h1>
            <StatusMark status={current.status} />
            <p className="mt-3 text-sm text-muted">{current.summary}</p>
            <dl className="mt-4">
              {current.metrics.map((metric) => (
                <div key={metric.label} className="flex justify-between gap-3 border-b border-line py-2 text-sm">
                  <dt className="text-muted">{metric.label}</dt>
                  <dd className="num text-right">{metric.value || "unavailable"}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-4 text-xs text-muted">Last activity {formatTime(current.activity_at)}</p>
          </>
        ) : (
          <p className="mt-3 text-sm text-muted">{topology.isError ? (topology.error as Error).message : "Waiting for topology."}</p>
        )}
      </aside>
      <div className="timeline" aria-label="Recent operations">
        {(timeline.data?.events ?? []).map((event) => (
          <article key={event.id} className="min-w-64 panel-block">
            <p className="kicker">{event.kind}</p>
            <h3 className="text-sm">{event.title}</h3>
            <p className="text-xs text-muted">{event.detail}</p>
            <p className="num mt-2 text-xs">{formatTime(event.at)}</p>
          </article>
        ))}
        {timeline.data?.events.length === 0 && <p className="text-sm text-muted">No operational events yet.</p>}
      </div>
    </section>
  );
}
