import { useState } from "react";
import type { TaskRow } from "../api";
import { KIND_LABEL, taskStatusColor } from "../status";
import { relativeTime } from "../util/time";
import { IconExternal } from "../icons";
import { EmptyState, StatusDot, TieredDisclosure } from "../ui";

// MilestoneTasks — the MILESTONE tier of the spine (ADR 0008 P1, PR-D). A
// milestone is a *view*: a goal's tasks grouped by their plan_key (§5.1, no new
// entity). Active milestones (any pending/running task) render in full; fully
// settled ones fold — the spine's active-shown/settled-folded rule at this tier.
// Each task row drills in (click to expand its details + PR link).

const ACTIVE = new Set(["pending", "running"]);

interface Milestone {
  key: string;              // plan_key, or "" for standalone tasks
  label: string;            // milestone label ?? plan_key ?? "Standalone"
  tasks: TaskRow[];
  active: boolean;          // any task pending/running
}

function groupByMilestone(tasks: TaskRow[]): Milestone[] {
  const byKey = new Map<string, Milestone>();
  for (const t of tasks) {
    const key = t.planKey ?? "";
    let m = byKey.get(key);
    if (!m) {
      m = { key, label: key || "Standalone tasks", tasks: [], active: false };
      byKey.set(key, m);
    }
    // Prefer a human milestone label when any task in the group carries one.
    if (t.milestone) m.label = t.milestone;
    m.tasks.push(t);
    if (ACTIVE.has(t.status)) m.active = true;
  }
  // Active milestones first; standalone bucket last within its band.
  return [...byKey.values()].sort((a, b) => Number(b.active) - Number(a.active));
}

function statusSummary(tasks: TaskRow[]): string {
  const by: Record<string, number> = {};
  for (const t of tasks) by[t.status] = (by[t.status] ?? 0) + 1;
  return Object.entries(by).map(([s, n]) => `${n} ${s}`).join(" · ");
}

export function MilestoneTasks({ tasks, emptyLabel }: { tasks: TaskRow[]; emptyLabel: string }) {
  if (tasks.length === 0) return <EmptyState title={emptyLabel} />;

  const milestones = groupByMilestone(tasks);
  const active = milestones.filter((m) => m.active);
  const settled = milestones.filter((m) => !m.active);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {active.map((m) => <MilestoneBlock key={m.key} m={m} />)}
      {settled.length > 0 && (
        <TieredDisclosure label="Completed milestones" count={settled.length}>
          <div style={{ display: "flex", flexDirection: "column", gap: 14, marginTop: 6 }}>
            {settled.map((m) => <MilestoneBlock key={m.key} m={m} />)}
          </div>
        </TieredDisclosure>
      )}
    </div>
  );
}

function MilestoneBlock({ m }: { m: Milestone }) {
  return (
    <div className="card" style={{ overflow: "hidden" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 16px", borderBottom: "1px solid var(--border)" }}>
        <StatusDot color={m.active ? "var(--accent)" : "var(--text-muted)"} live={m.active} />
        <span style={{ fontSize: 13, fontWeight: 600 }}>{m.label}</span>
        <span className="mono muted" style={{ marginLeft: "auto", fontSize: 11 }}>{statusSummary(m.tasks)}</span>
      </div>
      {m.tasks.map((t) => <TaskLine key={t.id} t={t} />)}
    </div>
  );
}

function TaskLine({ t }: { t: TaskRow }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ borderBottom: "1px solid var(--border)" }}>
      <div
        onClick={() => setOpen((o) => !o)}
        style={{
          display: "grid",
          gridTemplateColumns: "110px 110px minmax(0,1fr) 90px",
          gap: 12,
          alignItems: "center",
          padding: "10px 16px",
          cursor: "pointer",
        }}
      >
        <span className="mono secondary" style={{ fontSize: 12 }}>{KIND_LABEL[t.kind] ?? t.kind}</span>
        <span style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12.5 }}>
          <StatusDot color={taskStatusColor(t.status)} live={t.status === "running"} />
          {t.status}
        </span>
        <span className="mono truncate muted" style={{ fontSize: 11.5 }} title={t.id}>{t.id}</span>
        <span className="mono secondary" style={{ textAlign: "right", fontSize: 12 }}>{relativeTime(t.createdAt)}</span>
      </div>
      {open && (
        <div style={{ padding: "4px 16px 12px", display: "flex", flexDirection: "column", gap: 5 }}>
          <Detail label="Task id" value={t.id} mono />
          <Detail label="Workspace" value={t.workspaceDir} mono />
          {t.milestone && <Detail label="Milestone" value={t.milestone} />}
          {t.planKey && <Detail label="Plan key" value={t.planKey} mono />}
          <Detail label="Created" value={relativeTime(t.createdAt)} />
          {t.completedAt && <Detail label="Completed" value={relativeTime(t.completedAt)} />}
          {t.prUrl && (
            <div style={{ display: "flex", gap: 6, alignItems: "baseline" }}>
              <span className="eyebrow" style={{ minWidth: 84 }}>PR</span>
              <a href={t.prUrl} target="_blank" rel="noreferrer" style={{ fontSize: 12.5, display: "inline-flex", alignItems: "center", gap: 4 }}>
                {t.prUrl.replace("https://github.com/", "")} <IconExternal size={12} />
              </a>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Detail({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div style={{ display: "flex", gap: 6, alignItems: "baseline" }}>
      <span className="eyebrow" style={{ minWidth: 84 }}>{label}</span>
      <span className={mono ? "mono secondary truncate" : "secondary truncate"} style={{ fontSize: 12.5 }} title={value}>{value}</span>
    </div>
  );
}
