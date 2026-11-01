import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Background, Handle, Position, ReactFlow, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { api } from "../api";
import { Notice, Panel, StatusMark } from "../components/ui";
import { shortHash } from "../format";
import { useApp } from "../store";

type Pod = { name: string; phase: string; ready: boolean; restarts: number; image: string; namespace: string };
type Deployment = { name: string; desired: number; ready: number; available: number; image: string };
type Job = { name: string; succeeded: number; failed: number; active: number };
type Cluster = {
  connected: boolean;
  mode: string;
  reason: string;
  pods: Pod[];
  deployments: Deployment[];
  jobs: Job[];
  metrics_server: boolean;
  autoscaler_note?: string;
  local_router: { runtime: string; weight: number; stable: { version?: number; sha256?: string } | null; candidate: { version?: number } | null };
};

type ResourceData = { title: string; detail: string; status: string };

function ResourceNode({ data }: NodeProps) {
  const node = data as ResourceData;
  return (
    <article className={`stage resource ${node.status}`}>
      <Handle type="target" position={Position.Top} />
      <h3 className="text-sm">{node.title}</h3>
      <p className="mt-1 text-xs text-muted">{node.detail}</p>
      <Handle type="source" position={Position.Bottom} />
    </article>
  );
}

const nodeTypes = { resource: ResourceNode };

export function ClusterWorkspace() {
  const role = useApp((state) => state.user?.role);
  const cluster = useQuery({ queryKey: ["cluster"], queryFn: () => api<Cluster>("/api/v1/cluster") });
  const [pod, setPod] = useState("");
  const logs = useQuery({
    queryKey: ["logs", pod],
    queryFn: () => api<{ available: boolean; log: string }>(`/api/v1/cluster/logs?namespace=kubeml&pod=${pod}`),
    enabled: Boolean(pod) && role !== "observer",
  });
  const data = cluster.data;
  const graph = useMemo(() => buildGraph(data), [data]);

  return (
    <div className="workspace stack gap-3">
      <Panel title="Kubernetes">
        {data?.connected ? <StatusMark status="healthy" /> : <StatusMark status="unavailable" />}
        <p className="mt-3 text-sm text-muted">{data?.connected ? `${data.pods.length} pods visible in the kubeml namespace.` : data?.reason || "Cluster state is unavailable."}</p>
        <p className="mt-2 text-sm">{metricsNote(data)}</p>
        <div className="mt-4 h-[480px] overflow-hidden rounded-2xl border border-line">
          <ReactFlow nodes={graph.nodes} edges={graph.edges} nodeTypes={nodeTypes} fitView proOptions={{ hideAttribution: true }} onNodeClick={(_event, node) => { if (node.id.startsWith("pod:")) setPod(node.id.slice(4)); }}>
            <Background color="var(--line)" gap={28} />
          </ReactFlow>
        </div>
      </Panel>
      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="Pods">
          <table className="table">
            <thead><tr><th>Pod</th><th>Phase</th><th>Restarts</th><th>Image</th></tr></thead>
            <tbody>
              {(data?.pods ?? []).map((item) => (
                <tr key={item.name}><td><button className="quiet" type="button" onClick={() => setPod(item.name)}>{item.name}</button></td><td>{item.phase}</td><td className="num">{item.restarts}</td><td className="num text-xs">{item.image}</td></tr>
              ))}
            </tbody>
          </table>
          {data?.connected && data.pods.length === 0 && <Notice>The cluster is connected and the kubeml namespace has no pods.</Notice>}
          {(data?.deployments.length ?? 0) > 0 && (
            <div className="mt-3">
              {data?.deployments.map((item) => (
                <p key={item.name} className="border-b border-line py-2 text-sm">{item.name} · desired {item.desired} · ready {item.ready} · {item.image}</p>
              ))}
            </div>
          )}
          {(data?.jobs.length ?? 0) > 0 && (
            <div className="mt-3">
              {data?.jobs.map((item) => (
                <p key={item.name} className="border-b border-line py-2 text-sm">{item.name} · active {item.active} · succeeded {item.succeeded} · failed {item.failed}</p>
              ))}
            </div>
          )}
        </Panel>
        <div className="grid gap-3">
          <Panel title="Local router">
            <p className="text-sm">This block describes the process that is serving when Kubernetes is not the runtime.</p>
            <p className="num mt-2 text-sm">Weight {data?.local_router.weight ?? 0}. Stable v{data?.local_router.stable?.version ?? "—"}. Candidate {data?.local_router.candidate ? `v${data.local_router.candidate.version}` : "not loaded"}.</p>
            <p className="num mt-2 text-xs">{shortHash(data?.local_router.stable?.sha256)}</p>
          </Panel>
          <Panel title="Pod log">
            {role === "observer" ? <Notice>Observers cannot read pod logs.</Notice> : logs.data ? <pre className="num whitespace-pre-wrap text-xs">{logs.data.log || "No log lines."}</pre> : <Notice>Select a pod after the cluster is connected. Shell access is not available.</Notice>}
          </Panel>
        </div>
      </div>
    </div>
  );
}

function metricsNote(data: Cluster | undefined): string {
  if (!data) return "Waiting for the cluster probe.";
  if (!data.connected) return "metrics-server was not observed because the cluster probe did not succeed.";
  return data.autoscaler_note || (data.metrics_server
    ? "metrics-server is available, so CPU-based HPA can observe utilisation."
    : "metrics-server is not available. Replica counts can still be set, but HPA is not observing CPU.");
}

function buildGraph(data: Cluster | undefined): { nodes: Node[]; edges: { id: string; source: string; target: string; style: { stroke: string } }[] } {
  const stroke = { stroke: "var(--line)" };
  if (!data) return { nodes: [], edges: [] };
  const deployments = data.deployments ?? [];
  const pods = data.pods ?? [];
  const jobs = data.jobs ?? [];
  const nodes: Node[] = [{
    id: "namespace",
    type: "resource",
    position: { x: 280, y: 0 },
    data: {
      title: data.connected ? "namespace kubeml" : "cluster probe",
      detail: data.connected ? `${data.pods.length} pods` : data.reason || "unavailable",
      status: data.connected ? "healthy" : "unavailable",
    },
  }];
  const edges: { id: string; source: string; target: string; style: { stroke: string } }[] = [];
  deployments.forEach((item, index) => {
    const id = `deploy:${item.name}`;
    nodes.push({
      id,
      type: "resource",
      position: { x: index * 230, y: 160 },
      data: { title: item.name, detail: `ready ${item.ready}/${item.desired}`, status: item.ready === item.desired ? "healthy" : "degraded" },
    });
    edges.push({ id: `namespace-${id}`, source: "namespace", target: id, style: stroke });
  });
  pods.forEach((item, index) => {
    const id = `pod:${item.name}`;
    const parent = deployments.find((deployment) => item.name.startsWith(deployment.name));
    nodes.push({
      id,
      type: "resource",
      position: { x: (index % 4) * 230, y: 340 + Math.floor(index / 4) * 140 },
      data: { title: item.name, detail: `${item.phase} · restarts ${item.restarts}`, status: item.ready ? "healthy" : "degraded" },
    });
    edges.push({ id: `${parent ? `deploy:${parent.name}` : "namespace"}-${id}`, source: parent ? `deploy:${parent.name}` : "namespace", target: id, style: stroke });
  });
  jobs.forEach((item, index) => {
    const id = `job:${item.name}`;
    nodes.push({
      id,
      type: "resource",
      position: { x: 760, y: 160 + index * 140 },
      data: { title: item.name, detail: `active ${item.active} · failed ${item.failed}`, status: item.failed ? "failed" : "healthy" },
    });
    edges.push({ id: `namespace-${id}`, source: "namespace", target: id, style: stroke });
  });
  nodes.push({
    id: "metrics",
    type: "resource",
    position: { x: 0, y: 0 },
    data: {
      title: "metrics-server",
      detail: data.connected ? (data.metrics_server ? "available" : "not installed") : "not observed",
      status: data.metrics_server ? "healthy" : "unavailable",
    },
  });
  edges.push({ id: "namespace-metrics", source: "namespace", target: "metrics", style: stroke });
  nodes.push({
    id: "router",
    type: "resource",
    position: { x: 560, y: 0 },
    data: {
      title: "local router",
      detail: `stable v${data.local_router.stable?.version ?? "—"} · weight ${data.local_router.weight}`,
      status: data.local_router.stable ? "healthy" : "unavailable",
    },
  });
  return { nodes, edges };
}
