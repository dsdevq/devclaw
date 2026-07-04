import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { fetchProject, type GoalRow, type ProjectDetail as PD } from "../api";
import { mono, palettes } from "../theme";
import { relativeTime } from "../util/time";
import { TasksSection } from "../components/TasksSection";

// Matches Project Detail.dc.html: 56px breadcrumb header, big project name +
// live dot, GitHub/preview link row, Active goals table, collapsed Archived
// section (sessionStorage-persisted).

const VERSION_LABEL = "v0.7.2";

function archivedKey(projectId: string): string {
  return `devclaw:${projectId}:archivedExpanded`;
}

export function ProjectDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const p = palettes.dark;
  const [data, setData] = useState<PD | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [archivedExpanded, setArchivedExpanded] = useState<boolean>(() => {
    if (!id) return false;
    try {
      return sessionStorage.getItem(archivedKey(id)) === "1";
    } catch {
      return false;
    }
  });
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [hoveredKey, setHoveredKey] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    let alive = true;
    setData(null);
    setErr(null);
    fetchProject(id)
      .then((d) => alive && setData(d))
      .catch((e) => alive && setErr(String(e)));
    return () => {
      alive = false;
    };
  }, [id]);

  const toggleArchived = () => {
    setArchivedExpanded((prev) => {
      const next = !prev;
      if (id) {
        try {
          sessionStorage.setItem(archivedKey(id), next ? "1" : "0");
        } catch {
          /* ignore quota / disabled storage */
        }
      }
      return next;
    });
  };

  const phaseMeta = useMemo(
    () => ({
      executing: { color: p.accent },
      verifying: { color: p.accent },
      in_flight: { color: p.accent },
      idle: { color: p.textSecondary },
      firming: { color: p.accent },
      investigating: { color: p.accent },
      blocked: { color: p.red },
      achieved: { color: p.green },
      done: { color: p.green },
      cancelled: { color: p.red },
      error: { color: p.red },
    }),
    [p],
  );

  const gridCols = "240px 130px minmax(0,1fr) 110px";
  const rowHeight = 48;

  const goalsHeader = (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: gridCols,
        gap: 16,
        alignItems: "center",
        height: 32,
        boxSizing: "border-box",
        paddingLeft: 6,
        borderBottom: `1px solid ${p.borderStrong}`,
      }}
    >
      <HeaderCell text="Goal" color={p.textSecondary} />
      <HeaderCell text="Phase" color={p.textSecondary} />
      <HeaderCell text="In-flight action" color={p.textSecondary} />
      <HeaderCell text="Last update" color={p.textSecondary} align="right" />
    </div>
  );

  const buildRow = (goal: GoalRow, section: "active" | "archived") => {
    const key = `${section}:${goal.id}`;
    const meta = (phaseMeta as Record<string, { color: string }>)[
      goal.phase ?? ""
    ] || { color: p.textMuted };
    const isSelected = selectedKey === key;
    const isHovered = hoveredKey === key && !isSelected;
    return (
      <div
        key={key}
        onClick={() => {
          setSelectedKey(key);
          navigate(`/goals/${goal.id}`);
        }}
        onMouseEnter={() => setHoveredKey(key)}
        onMouseLeave={() => setHoveredKey(null)}
        style={{
          display: "grid",
          gridTemplateColumns: gridCols,
          gap: 16,
          alignItems: "center",
          height: rowHeight,
          boxSizing: "border-box",
          borderBottom: `1px solid ${p.border}`,
          borderLeft: `2px solid ${isSelected ? p.accent : "transparent"}`,
          paddingLeft: 6,
          cursor: "pointer",
          background: isSelected
            ? p.rowSelectedBg
            : isHovered
              ? p.rowHover
              : "transparent",
          transition: "background 0.1s ease",
        }}
      >
        <div
          style={{
            fontFamily: mono,
            fontSize: 13,
            fontWeight: 500,
            color: p.textPrimary,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {goal.id}
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 7,
            fontSize: 13,
            color: p.textPrimary,
          }}
        >
          <span
            style={{
              width: 6,
              height: 6,
              borderRadius: "50%",
              background: meta.color,
              flexShrink: 0,
            }}
          />
          {goal.phaseLabel}
        </div>
        <div
          style={{
            fontFamily: mono,
            fontSize: 12.5,
            color: p.textSecondary,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {goal.action}
        </div>
        <div
          style={{
            fontFamily: mono,
            fontSize: 12.5,
            textAlign: "right",
            color: p.textSecondary,
          }}
        >
          {relativeTime(goal.lastUpdateMs)}
        </div>
      </div>
    );
  };

  const statusDot = () => {
    if (!data) return null;
    const color =
      data.status === "active"
        ? p.green
        : data.status === "paused"
          ? p.amber
          : p.textMuted;
    const label =
      data.status.charAt(0).toUpperCase() + data.status.slice(1);
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 7,
          fontSize: 13,
          color: p.textPrimary,
        }}
      >
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: color,
            flexShrink: 0,
          }}
        />
        {label}
      </div>
    );
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        width: "100%",
        boxSizing: "border-box",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        style={{
          height: 56,
          flexShrink: 0,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "0 40px",
          boxSizing: "border-box",
          borderBottom: `1px solid ${p.border}`,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <div style={{ fontSize: 14, fontWeight: 600, letterSpacing: "-0.01em" }}>
            devclaw console
          </div>
          <div style={{ width: 1, height: 16, background: p.border }} />
          <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
            <Link
              to="/"
              style={{ color: p.textSecondary, textDecoration: "none" }}
            >
              projects
            </Link>
            <span style={{ color: p.textMuted }}>/</span>
            <span style={{ color: p.textPrimary }}>{id}</span>
          </div>
        </div>
        <div style={{ fontFamily: mono, fontSize: 12, color: p.textMuted }}>
          {VERSION_LABEL}
        </div>
      </div>

      {err && (
        <div style={{ padding: "22px 40px", fontSize: 13, color: p.red }}>
          {err}
        </div>
      )}

      {!data && !err && (
        <div style={{ padding: "22px 40px", fontSize: 13, color: p.textMuted }}>
          Loading…
        </div>
      )}

      {data && (
        <>
          <div
            style={{
              padding: "32px 40px 24px",
              boxSizing: "border-box",
              borderBottom: `1px solid ${p.border}`,
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 14,
                marginBottom: 14,
              }}
            >
              <div
                style={{
                  fontFamily: mono,
                  fontSize: 26,
                  fontWeight: 600,
                  letterSpacing: "-0.01em",
                  color: p.textPrimary,
                }}
              >
                {data.name}
              </div>
              {statusDot()}
            </div>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 28,
                flexWrap: "wrap",
              }}
            >
              {data.repoUrl && (
                <MetaLink label="GitHub" url={data.repoUrl} palette={p} />
              )}
              {data.previewUrl && (
                <MetaLink label="Live preview" url={data.previewUrl} palette={p} />
              )}
              {!data.repoUrl && !data.previewUrl && (
                <span style={{ fontSize: 12, color: p.textMuted }}>
                  no repo/preview registered
                </span>
              )}
            </div>
          </div>

          <div
            style={{
              flex: 1,
              overflow: "auto",
              padding: "0 40px 60px",
              boxSizing: "border-box",
            }}
          >
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
                  Active goals
                </span>
                <span style={{ fontFamily: mono, fontSize: 11, color: p.textMuted }}>
                  ({data.active.length})
                </span>
              </div>
              {data.active.length > 0 ? (
                <>
                  {goalsHeader}
                  {data.active.map((g) => buildRow(g, "active"))}
                </>
              ) : (
                <div
                  style={{
                    padding: "22px 6px",
                    fontSize: 13,
                    color: p.textMuted,
                  }}
                >
                  No active goals — file one from a devclaw session or Telegram
                </div>
              )}
            </div>

            <div style={{ marginTop: 36 }}>
              <div
                onClick={toggleArchived}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 9,
                  cursor: "pointer",
                  padding: "6px 0",
                  userSelect: "none",
                  width: "fit-content",
                }}
              >
                <span
                  style={{
                    display: "inline-block",
                    width: 0,
                    height: 0,
                    borderStyle: "solid",
                    borderWidth: "4px 0 4px 6px",
                    borderColor: `transparent transparent transparent ${p.textSecondary}`,
                    transition: "transform 0.15s ease",
                    transform: `rotate(${archivedExpanded ? 90 : 0}deg)`,
                  }}
                />
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    letterSpacing: "0.06em",
                    textTransform: "uppercase",
                    color: p.textSecondary,
                  }}
                >
                  Archived goals
                </span>
                <span style={{ fontFamily: mono, fontSize: 11, color: p.textMuted }}>
                  ({data.archived.length})
                </span>
              </div>
              {archivedExpanded && data.archived.length > 0 && (
                <>
                  {goalsHeader}
                  {data.archived.map((g) => buildRow(g, "archived"))}
                </>
              )}
            </div>

            <TasksSection
              title="Recent tasks"
              tasks={data.tasks ?? []}
              emptyLabel="No standalone tasks — dispatched features/fixes without a goal will land here"
            />
          </div>
        </>
      )}
    </div>
  );
}

function MetaLink({
  label,
  url,
  palette: p,
}: {
  label: string;
  url: string;
  palette: (typeof palettes)["dark"];
}) {
  return (
    <div style={{ display: "flex", alignItems: "baseline", gap: 7 }}>
      <span
        style={{
          fontSize: 11,
          color: p.textSecondary,
          textTransform: "uppercase",
          letterSpacing: "0.04em",
        }}
      >
        {label}
      </span>
      <a
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        style={{
          fontFamily: mono,
          fontSize: 12.5,
          color: p.textPrimary,
          textDecoration: "none",
        }}
      >
        {url.replace(/^https?:\/\//, "")}
      </a>
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
