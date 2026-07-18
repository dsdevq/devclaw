import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchProjects, tokenQueryString, type ProjectRow } from "../api";
import { relativeTime } from "../util/time";
import { EmptyState, ErrorNote, Loading, StatusDot } from "../ui";

const STATUS: Record<ProjectRow["status"], { label: string; color: string }> = {
  active: { label: "Active", color: "var(--green)" },
  paused: { label: "Paused", color: "var(--amber)" },
  archived: { label: "Archived", color: "var(--text-muted)" },
};

const COLS = "minmax(0,1fr) 120px 110px 130px";

export function Projects() {
  const nav = useNavigate();
  const qs = tokenQueryString();
  const [rows, setRows] = useState<ProjectRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchProjects()
      .then((r) => alive && setRows(r))
      .catch((e) => alive && setErr(String(e)));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="page">
      <h1 style={{ fontSize: 22, fontWeight: 650, letterSpacing: "-0.02em", margin: "0 0 4px" }}>
        Projects
      </h1>
      <p className="secondary" style={{ margin: "0 0 22px", fontSize: 13.5 }}>
        Every repository devclaw is driving.
      </p>

      {err && <ErrorNote>{err}</ErrorNote>}
      {!rows && !err && <Loading />}
      {rows && rows.length === 0 && (
        <EmptyState title="No projects registered" hint="Register one from a devclaw session." />
      )}

      {rows && rows.length > 0 && (
        <div className="card" style={{ overflow: "hidden" }}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: COLS,
              gap: 16,
              padding: "10px 16px",
              borderBottom: "1px solid var(--border)",
            }}
          >
            <span className="eyebrow">Project</span>
            <span className="eyebrow">Status</span>
            <span className="eyebrow" style={{ textAlign: "right" }}>Goals</span>
            <span className="eyebrow" style={{ textAlign: "right" }}>Activity</span>
          </div>
          {rows.map((r) => {
            const s = STATUS[r.status];
            return (
              <div
                key={r.id}
                className="rowlink"
                onClick={() => nav(`/projects/${r.id}${qs}`)}
                style={{
                  display: "grid",
                  gridTemplateColumns: COLS,
                  gap: 16,
                  alignItems: "center",
                  padding: "13px 16px",
                  borderBottom: "1px solid var(--border)",
                }}
              >
                <span className="truncate" style={{ fontWeight: 550 }}>{r.name}</span>
                <span style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
                  <StatusDot color={s.color} live={r.status === "active"} />
                  {s.label}
                </span>
                <span
                  className="mono"
                  style={{
                    textAlign: "right",
                    fontSize: 13,
                    color: r.activeGoals ? "var(--text)" : "var(--text-muted)",
                  }}
                >
                  {r.activeGoals}
                </span>
                <span className="mono secondary" style={{ textAlign: "right", fontSize: 12.5 }}>
                  {relativeTime(r.lastActivityMs)}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
