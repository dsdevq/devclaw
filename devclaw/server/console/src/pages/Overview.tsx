import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchAllGoals, tokenQueryString, type GoalWithProject } from "../api";
import { phaseColor, phaseIsLive } from "../status";
import { relativeTime } from "../util/time";
import { EmptyState, ErrorNote, Loading, SectionLabel, StatusDot } from "../ui";

export function Overview() {
  const nav = useNavigate();
  const qs = tokenQueryString();
  const [rows, setRows] = useState<GoalWithProject[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => fetchAllGoals().then((r) => alive && setRows(r)).catch((e) => alive && setErr(String(e)));
    load();
    const t = setInterval(load, 20000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const live = (rows ?? []).filter((g) => phaseIsLive(g.phase));
  const blocked = (rows ?? []).filter((g) => g.phase === "blocked");
  const projects = new Set((rows ?? []).map((g) => g.projectId)).size;
  const recent = [...(rows ?? [])]
    .filter((g) => !g.archived)
    .sort((a, b) => (b.lastUpdateMs ?? 0) - (a.lastUpdateMs ?? 0))
    .slice(0, 6);

  const open = (id: string) => nav(`/goals/${id}${qs}`);

  return (
    <div className="page">
      <h1 style={{ fontSize: 22, fontWeight: 650, letterSpacing: "-0.02em", margin: "0 0 18px" }}>
        Overview
      </h1>

      {err && <ErrorNote>{err}</ErrorNote>}
      {!rows && !err && <Loading />}

      {rows && (
        <>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 30 }}>
            <Stat label="Projects" value={projects} />
            <Stat label="Running" value={live.length} color="var(--accent)" live={live.length > 0} />
            <Stat label="Needs you" value={blocked.length} color={blocked.length ? "var(--amber)" : "var(--text-muted)"} />
          </div>

          <section style={{ marginBottom: 34 }}>
            <SectionLabel count={blocked.length}>Needs you</SectionLabel>
            {blocked.length === 0 ? (
              <div className="card">
                <EmptyState title="All clear" hint="Nothing is waiting on you right now." />
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {blocked.map((g) => (
                  <GoalCard key={g.id} g={g} onOpen={() => open(g.id)} tone="amber" />
                ))}
              </div>
            )}
          </section>

          <section style={{ marginBottom: 34 }}>
            <SectionLabel count={live.length}>Running now</SectionLabel>
            {live.length === 0 ? (
              <div className="card">
                <EmptyState title="Idle" hint="No goals are executing at the moment." />
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {live.map((g) => (
                  <GoalCard key={g.id} g={g} onOpen={() => open(g.id)} tone="accent" />
                ))}
              </div>
            )}
          </section>

          <section>
            <SectionLabel>Recently active</SectionLabel>
            <div className="card" style={{ overflow: "hidden" }}>
              {recent.map((g) => (
                <div
                  key={g.id}
                  className="rowlink"
                  onClick={() => open(g.id)}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    padding: "12px 16px",
                    borderBottom: "1px solid var(--border)",
                  }}
                >
                  <StatusDot color={phaseColor(g.phase)} live={phaseIsLive(g.phase)} />
                  <span className="mono truncate" style={{ flex: 1, fontSize: 13 }}>{g.id}</span>
                  <span className="secondary truncate" style={{ fontSize: 12, maxWidth: 160 }}>{g.projectName}</span>
                  <span className="mono muted" style={{ fontSize: 12 }}>{relativeTime(g.lastUpdateMs)}</span>
                </div>
              ))}
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function Stat({ label, value, color, live }: { label: string; value: number; color?: string; live?: boolean }) {
  return (
    <div className="card" style={{ padding: "14px 18px", minWidth: 130, flex: "1 1 130px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        {color && <StatusDot color={color} live={live} />}
        <span style={{ fontSize: 26, fontWeight: 650, letterSpacing: "-0.02em" }}>{value}</span>
      </div>
      <div className="eyebrow" style={{ marginTop: 4 }}>{label}</div>
    </div>
  );
}

function GoalCard({ g, onOpen, tone }: { g: GoalWithProject; onOpen: () => void; tone: "amber" | "accent" }) {
  const border = tone === "amber" ? "var(--amber)" : "var(--accent)";
  return (
    <div
      className="card rowlink"
      onClick={onOpen}
      style={{ padding: "14px 16px", borderLeft: `2px solid ${border}` }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
        <StatusDot color={phaseColor(g.phase)} live={phaseIsLive(g.phase)} />
        <span className="mono" style={{ fontWeight: 550, fontSize: 13.5 }}>{g.id}</span>
        <span className="secondary" style={{ fontSize: 12 }}>· {g.projectName}</span>
        <span className="badge" style={{ marginLeft: "auto" }}>{g.phaseLabel}</span>
      </div>
      <div className="secondary truncate" style={{ fontSize: 12.5, paddingLeft: 17 }}>
        {g.action || "—"}
      </div>
    </div>
  );
}
