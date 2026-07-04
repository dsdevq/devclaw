import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchProjects, type ProjectRow } from "../api";
import { mono, palettes } from "../theme";
import { relativeTime } from "../util/time";

// Mirrors the Claude Design mock at Projects Home.dc.html verbatim: same
// palette, same 56px header, same grid columns, same arrow-key nav.

type Status = ProjectRow["status"];

const STATUS_LABEL: Record<Status, string> = {
  active: "Active",
  paused: "Paused",
  archived: "Archived",
};

const VERSION_LABEL = "v0.7.2";

export function ProjectsHome() {
  const p = palettes.dark;
  const navigate = useNavigate();
  const [rows, setRows] = useState<ProjectRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<number>(-1);
  const [hovered, setHovered] = useState<number>(-1);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    fetchProjects()
      .then((r) => alive && setRows(r))
      .catch((e) => alive && setErr(String(e)));
    return () => {
      alive = false;
    };
  }, []);

  const statusMeta = useMemo(
    () => ({
      active: { color: p.green },
      paused: { color: p.amber },
      archived: { color: p.textMuted },
    }),
    [p],
  );

  const gridCols = "minmax(0,1fr) 120px 130px 130px";
  const rowHeight = 48;

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!rows || rows.length === 0) return;
    const max = rows.length - 1;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelected((i) => Math.min(i + 1, max));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelected((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter" && selected >= 0 && rows[selected]) {
      e.preventDefault();
      navigate(`/projects/${rows[selected].id}`);
    }
  };

  return (
    <div
      style={{
        height: "100vh",
        width: "100%",
        boxSizing: "border-box",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
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
        <div style={{ fontSize: 14, fontWeight: 600, letterSpacing: "-0.01em" }}>
          devclaw console
        </div>
        <div style={{ fontFamily: mono, fontSize: 12, color: p.textMuted }}>
          {VERSION_LABEL}
        </div>
      </div>

      <div
        ref={containerRef}
        style={{
          flex: 1,
          overflow: "auto",
          padding: "0 40px",
          boxSizing: "border-box",
        }}
      >
        {rows === null && err === null && (
          <div style={{ padding: "22px 6px", fontSize: 13, color: p.textMuted }}>
            Loading…
          </div>
        )}
        {err !== null && (
          <div style={{ padding: "22px 6px", fontSize: 13, color: p.red }}>
            {err}
          </div>
        )}
        {rows !== null && rows.length === 0 && (
          <div
            style={{
              height: "calc(100vh - 56px)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <div style={{ fontSize: 13, color: p.textMuted }}>
              No projects registered yet
            </div>
          </div>
        )}

        {rows !== null && rows.length > 0 && (
          <div
            tabIndex={0}
            onKeyDown={handleKeyDown}
            style={{ outline: "none", marginTop: 8 }}
          >
            <div
              style={{
                display: "grid",
                gridTemplateColumns: gridCols,
                gap: 16,
                alignItems: "center",
                height: 32,
                boxSizing: "border-box",
                paddingLeft: 6,
                position: "sticky",
                top: 0,
                background: p.bg,
                borderBottom: `1px solid ${p.borderStrong}`,
              }}
            >
              <HeaderCell text="Project" color={p.textSecondary} />
              <HeaderCell text="Status" color={p.textSecondary} />
              <HeaderCell text="Active goals" color={p.textSecondary} align="right" />
              <HeaderCell text="Last activity" color={p.textSecondary} align="right" />
            </div>

            {rows.map((r, i) => {
              const meta = statusMeta[r.status];
              const isSelected = selected === i;
              const isHovered = hovered === i && !isSelected;
              return (
                <div
                  key={r.id}
                  onClick={() => {
                    setSelected(i);
                    navigate(`/projects/${r.id}`);
                  }}
                  onMouseEnter={() => setHovered(i)}
                  onMouseLeave={() => setHovered(-1)}
                  style={{
                    display: "grid",
                    gridTemplateColumns: gridCols,
                    gap: 16,
                    alignItems: "center",
                    height: rowHeight,
                    boxSizing: "border-box",
                    borderBottom: `1px solid ${p.border}`,
                    borderLeft: `2px solid ${
                      isSelected ? p.accent : "transparent"
                    }`,
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
                      fontSize: 13.5,
                      fontWeight: 500,
                      color: p.textPrimary,
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {r.name}
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
                    {STATUS_LABEL[r.status]}
                  </div>
                  <div
                    style={{
                      fontFamily: mono,
                      fontSize: 13,
                      textAlign: "right",
                      color: r.activeGoals === 0 ? p.textMuted : p.textPrimary,
                    }}
                  >
                    {r.activeGoals}
                  </div>
                  <div
                    style={{
                      fontFamily: mono,
                      fontSize: 12.5,
                      textAlign: "right",
                      color: p.textSecondary,
                    }}
                  >
                    {relativeTime(r.lastActivityMs)}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
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
