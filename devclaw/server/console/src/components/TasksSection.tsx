import type { TaskRow } from "../api";
import { mono, palettes } from "../theme";
import { relativeTime } from "../util/time";

// Compact task table used in two places:
//   - ProjectDetail: standalone tasks in the project's workspace (no goal owns).
//   - GoalDetail:    tasks the goal heartbeat dispatched (parent_goal_id match).
// One shape, one renderer — labeling changes at the call site.

const KIND_LABEL: Record<string, string> = {
  implement_feature: "feature",
  fix_bug: "bug fix",
  review_repository: "review",
  onboard: "onboard",
};

export function TasksSection({
  title,
  tasks,
  emptyLabel,
}: {
  title: string;
  tasks: TaskRow[];
  emptyLabel: string;
}) {
  const p = palettes.dark;

  const statusColor = (s: string): string => {
    if (s === "done") return p.green;
    if (s === "running") return p.accent;
    if (s === "pending") return p.textSecondary;
    if (s === "failed") return p.red;
    if (s === "cancelled") return p.textMuted;
    return p.textMuted;
  };

  const gridCols = "100px 90px minmax(0,1fr) 110px";
  const rowHeight = 40;

  const header = (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: gridCols,
        gap: 16,
        alignItems: "center",
        height: 28,
        boxSizing: "border-box",
        paddingLeft: 6,
        borderBottom: `1px solid ${p.borderStrong}`,
      }}
    >
      <HeaderCell text="Kind" color={p.textSecondary} />
      <HeaderCell text="Status" color={p.textSecondary} />
      <HeaderCell text="Goal" color={p.textSecondary} />
      <HeaderCell text="Started" color={p.textSecondary} align="right" />
    </div>
  );

  return (
    <div style={{ marginTop: 28 }}>
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: 8,
          marginBottom: 6,
        }}
      >
        <span
          style={{
            fontSize: 11,
            fontWeight: 600,
            letterSpacing: "0.06em",
            textTransform: "uppercase",
            color: p.textSecondary,
          }}
        >
          {title}
        </span>
        <span style={{ fontFamily: mono, fontSize: 11, color: p.textMuted }}>
          ({tasks.length})
        </span>
      </div>
      {tasks.length === 0 ? (
        <div
          style={{
            padding: "18px 6px",
            fontSize: 13,
            color: p.textMuted,
          }}
        >
          {emptyLabel}
        </div>
      ) : (
        <>
          {header}
          {tasks.map((t) => (
            <div
              key={t.id}
              style={{
                display: "grid",
                gridTemplateColumns: gridCols,
                gap: 16,
                alignItems: "center",
                height: rowHeight,
                boxSizing: "border-box",
                borderBottom: `1px solid ${p.border}`,
                paddingLeft: 6,
              }}
            >
              <div
                style={{
                  fontFamily: mono,
                  fontSize: 12,
                  color: p.textSecondary,
                }}
              >
                {KIND_LABEL[t.kind] ?? t.kind}
              </div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 7,
                  fontSize: 12.5,
                  color: p.textPrimary,
                }}
              >
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: "50%",
                    background: statusColor(t.status),
                    flexShrink: 0,
                  }}
                />
                {t.status}
              </div>
              <div
                style={{
                  fontSize: 12.5,
                  color: p.textPrimary,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
                title={t.goal}
              >
                {t.goal}
              </div>
              <div
                style={{
                  fontFamily: mono,
                  fontSize: 12,
                  textAlign: "right",
                  color: p.textSecondary,
                }}
              >
                {relativeTime(t.createdAt)}
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

function HeaderCell({
  text,
  color,
  align,
}: {
  text: string;
  color: string;
  align?: "right";
}) {
  return (
    <div
      style={{
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: "0.06em",
        textTransform: "uppercase",
        color,
        textAlign: align,
      }}
    >
      {text}
    </div>
  );
}
