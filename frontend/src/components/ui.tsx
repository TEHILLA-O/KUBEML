import type { ReactNode } from "react";

export function StatusMark({ status }: { status: string }) {
  return (
    <span className={`status-mark status-${status}`}>
      <i aria-hidden="true" />
      {status.replaceAll("_", " ")}
    </span>
  );
}

export function Panel({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return (
    <section className="panel-block">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-sm font-medium">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

export function Notice({ children }: { children: ReactNode }) {
  return <p className="text-sm text-muted">{children}</p>;
}
