import type { TaskRow } from "../api";
import { KIND_LABEL, taskStatusColor } from "../status";
import { relativeTime } from "../util/time";
import { EmptyState, StatusDot } from "../ui";

// Compact task table — the goal's dispatched tasks, or a project's standalone
// tasks. One shape, labeled at the call site.

const COLS = "110px 110px minmax(0,1fr) 100px";

export function TasksSection({ tasks, emptyLabel }: { tasks: TaskRow[]; emptyLabel: string }) {
  if (tasks.length === 0) return <EmptyState title={emptyLabel} />;
  return (
    <div className="card" style={{ overflow: "hidden" }}>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: COLS,
          gap: 14,
          padding: "9px 16px",
          borderBottom: "1px solid var(--border)",
        }}
      >
        <span className="eyebrow">Kind</span>
        <span className="eyebrow">Status</span>
        <span className="eyebrow">Goal</span>
        <span className="eyebrow" style={{ textAlign: "right" }}>Started</span>
      </div>
      {tasks.map((t) => (
        <div
          key={t.id}
          style={{
            display: "grid",
            gridTemplateColumns: COLS,
            gap: 14,
            alignItems: "center",
            padding: "11px 16px",
            borderBottom: "1px solid var(--border)",
          }}
        >
          <span className="mono secondary" style={{ fontSize: 12 }}>{KIND_LABEL[t.kind] ?? t.kind}</span>
          <span style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12.5 }}>
            <StatusDot color={taskStatusColor(t.status)} live={t.status === "running"} />
            {t.status}
          </span>
          <span className="truncate" style={{ fontSize: 12.5 }} title={t.goal}>{t.goal}</span>
          <span className="mono secondary" style={{ textAlign: "right", fontSize: 12 }}>
            {relativeTime(t.createdAt)}
          </span>
        </div>
      ))}
    </div>
  );
}
