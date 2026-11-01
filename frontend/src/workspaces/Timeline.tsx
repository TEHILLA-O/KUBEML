import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { Notice, Panel, StatusMark } from "../components/ui";
import { formatTime } from "../format";

type Event = { id: string; kind: string; title: string; detail: string; at: string | null; status: string };
type Audit = { id: string; at: string; actor: string; action: string; target: string; reason: string; outcome: string };

export function TimelineWorkspace() {
  const timeline = useQuery({ queryKey: ["timeline"], queryFn: () => api<{ events: Event[] }>("/api/v1/timeline?limit=80") });
  const audit = useQuery({ queryKey: ["audit"], queryFn: () => api<{ events: Audit[] }>("/api/v1/audit") });
  return (
    <div className="workspace two">
      <Panel title="Operational timeline">
        {(timeline.data?.events ?? []).map((event) => (
          <article key={event.id} className="border-b border-line py-3">
            <div className="flex items-center justify-between"><span className="kicker">{event.kind}</span><StatusMark status={mark(event.status)} /></div>
            <h3 className="text-sm">{event.title}</h3>
            <p className="text-sm text-muted">{event.detail}</p>
            <p className="num text-xs">{formatTime(event.at)}</p>
          </article>
        ))}
        {timeline.data?.events.length === 0 && <Notice>No events recorded.</Notice>}
      </Panel>
      <Panel title="Audit log">
        {(audit.data?.events ?? []).map((event) => (
          <p key={event.id} className="border-b border-line py-2 text-sm"><span className="num">{formatTime(event.at)}</span> {event.actor} {event.action} {event.target} · {event.outcome}</p>
        ))}
      </Panel>
    </div>
  );
}

function mark(status: string): string {
  if (status === "succeeded" || status === "promoted" || status === "resolved") return "healthy";
  if (status === "failed" || status === "unresolved") return "failed";
  if (status === "open") return "degraded";
  return "idle";
}
