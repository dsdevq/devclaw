import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchAllGoals, tokenQueryString, type GoalWithProject } from "../api";
import { phaseColor, phaseIsLive } from "../status";
import { relativeTime } from "../util/time";
import { EmptyState, ErrorNote, Loading, StatusDot } from "../ui";

type Filter = "all" | "running" | "blocked" | "done";

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "running", label: "Running" },
  { id: "blocked", label: "Blocked" },
  { id: "done", label: "Done" },
];

function match(g: GoalWithProject, f: Filter): boolean {
  if (f === "all") return true;
  if (f === "running") return phaseIsLive(g.phase);
  if (f === "blocked") return g.phase === "blocked";
  return g.phase === "done" || g.phase === "achieved" || g.phase === "cancelled";
}

const COLS = "minmax(0,1.4fr) 150px 120px minmax(0,1fr) 110px";

export function Goals() {
  const nav = useNavigate();
  const qs = tokenQueryString();
  const [rows, setRows] = useState<GoalWithProject[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");

  useEffect(() => {
    let alive = true;
    fetchAllGoals()
      .then((r) => alive && setRows(r))
      .catch((e) => alive && setErr(String(e)));
    return () => {
      alive = false;
    };
  }, []);

  const counts = useMemo(() => {
    const c: Record<Filter, number> = { all: 0, running: 0, blocked: 0, done: 0 };
    for (const g of rows ?? []) for (const f of FILTERS) if (match(g, f.id)) c[f.id]++;
    return c;
  }, [rows]);

  const shown = (rows ?? []).filter((g) => match(g, filter));

  return (
    <div className="page">
      <h1 style={{ fontSize: 22, fontWeight: 650, letterSpacing: "-0.02em", margin: "0 0 4px" }}>
        Goals
      </h1>
      <p className="secondary" style={{ margin: "0 0 20px", fontSize: 13.5 }}>
        Every goal across all projects.
      </p>

      <div style={{ display: "flex", gap: 8, marginBottom: 18, flexWrap: "wrap" }}>
        {FILTERS.map((f) => (
          <button
            key={f.id}
            className={`btn sm${filter === f.id ? " primary" : ""}`}
            onClick={() => setFilter(f.id)}
          >
            {f.label}
            <span className="mono" style={{ opacity: 0.7, fontSize: 11 }}>{counts[f.id]}</span>
          </button>
        ))}
      </div>

      {err && <ErrorNote>{err}</ErrorNote>}
      {!rows && !err && <Loading />}
      {rows && shown.length === 0 && (
        <EmptyState title="Nothing here" hint="No goals match this filter." />
      )}

      {shown.length > 0 && (
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
            <span className="eyebrow">Goal</span>
            <span className="eyebrow">Project</span>
            <span className="eyebrow">Phase</span>
            <span className="eyebrow">Action</span>
            <span className="eyebrow" style={{ textAlign: "right" }}>Updated</span>
          </div>
          {shown.map((g) => (
            <div
              key={`${g.projectId}:${g.id}`}
              className="rowlink"
              onClick={() => nav(`/goals/${g.id}${qs}`)}
              style={{
                display: "grid",
                gridTemplateColumns: COLS,
                gap: 16,
                alignItems: "center",
                padding: "13px 16px",
                borderBottom: "1px solid var(--border)",
                opacity: g.archived ? 0.6 : 1,
              }}
            >
              <span className="mono truncate" style={{ fontWeight: 500, fontSize: 13 }}>{g.id}</span>
              <span className="truncate secondary" style={{ fontSize: 12.5 }}>{g.projectName}</span>
              <span style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12.5 }}>
                <StatusDot color={phaseColor(g.phase)} live={phaseIsLive(g.phase)} />
                {g.phaseLabel}
              </span>
              <span className="mono truncate secondary" style={{ fontSize: 12 }}>{g.action || "—"}</span>
              <span className="mono secondary" style={{ textAlign: "right", fontSize: 12.5 }}>
                {relativeTime(g.lastUpdateMs)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
