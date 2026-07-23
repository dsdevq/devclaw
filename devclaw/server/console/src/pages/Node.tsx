import { useEffect, useState } from "react";
import { fetchNode, type NodeLayer, type NodeVitals } from "../api";
import { relativeTime } from "../util/time";
import { ErrorNote, Loading, SectionLabel, StatusDot } from "../ui";
import { Fleet } from "../components/Fleet";

// The NODE view (ADR 0008 P1) — the top of the console drill-down spine. All
// read-only over /node.json: dispatch/heartbeat, goal population, the
// clean-cycle headline, and the 5-layer strip. Later P1 slices wire the tiers
// below (project → goal → milestone → task) into this screen.

function dispatchTone(d: NodeVitals["dispatch"]): { label: string; color: string; live: boolean } {
  if (d.operatorHold.on) return { label: "Paused (operator)", color: "var(--amber)", live: false };
  if (d.blocked && d.schedule.enabled) return { label: "Off-hours", color: "var(--amber)", live: false };
  if (d.blocked) return { label: "Quota held", color: "var(--amber)", live: false };
  return { label: "Running", color: "var(--green)", live: true };
}

function layerColor(status: NodeLayer["status"]): string {
  switch (status) {
    case "up":
    case "active":
      return "var(--green)";
    case "held":
    case "paused":
      return "var(--amber)";
    default:
      return "var(--text-muted)"; // idle / unknown — no positive signal
  }
}

export function Node() {
  const [v, setV] = useState<NodeVitals | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => fetchNode().then((r) => alive && setV(r)).catch((e) => alive && setErr(String(e)));
    load();
    const t = setInterval(load, 20000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const disp = v ? dispatchTone(v.dispatch) : null;

  return (
    <div className="page">
      <h1 style={{ fontSize: 22, fontWeight: 650, letterSpacing: "-0.02em", margin: "0 0 18px" }}>
        Node
      </h1>

      {err && <ErrorNote>{err}</ErrorNote>}
      {!v && !err && <Loading />}

      {v && disp && (
        <>
          {/* Heartbeat / dispatch state */}
          <div className="card" style={{ padding: "14px 18px", marginBottom: 24, display: "flex", alignItems: "center", gap: 10 }}>
            <StatusDot color={disp.color} live={disp.live} />
            <span style={{ fontSize: 15, fontWeight: 600 }}>{disp.label}</span>
            {v.dispatch.reason && <span className="secondary" style={{ fontSize: 12.5 }}>{v.dispatch.reason}</span>}
            <span className="mono muted" style={{ marginLeft: "auto", fontSize: 11 }}>v{v.version}</span>
          </div>

          {/* Goal population */}
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 30 }}>
            <Stat label="Goals" value={v.goals.total} />
            <Stat label="Running" value={v.goals.running} color="var(--accent)" live={v.goals.running > 0} />
            <Stat
              label="Needs you"
              value={v.goals.needsYou}
              color={v.goals.needsYou ? "var(--amber)" : "var(--text-muted)"}
            />
            <Stat label="Done" value={v.goals.done} color="var(--green)" />
            <Stat label="Running tasks" value={v.runningTasks} color={v.runningTasks ? "var(--accent)" : "var(--text-muted)"} live={v.runningTasks > 0} />
          </div>

          {/* The NODE→PROJECT→GOAL drill-down spine */}
          <Fleet />

          {/* Clean-cycle headline */}
          <section style={{ marginBottom: 30 }}>
            <SectionLabel>Clean-cycle rate</SectionLabel>
            <div className="card" style={{ padding: "14px 18px", display: "flex", alignItems: "center", gap: 10 }}>
              {v.cleanCycle.clean === null ? (
                <span className="secondary">No cycle reports yet.</span>
              ) : (
                <>
                  <StatusDot color={v.cleanCycle.clean ? "var(--green)" : "var(--amber)"} />
                  <span style={{ fontSize: 14, fontWeight: 600 }}>
                    Last window {v.cleanCycle.clean ? "clean" : "wedged"}
                  </span>
                  <span className="secondary" style={{ fontSize: 12.5 }}>
                    {v.cleanCycle.recent.clean}/{v.cleanCycle.recent.total} recent windows clean
                  </span>
                  {v.cleanCycle.lastWindowEndMs && (
                    <span className="mono muted" style={{ marginLeft: "auto", fontSize: 11 }}>
                      {relativeTime(v.cleanCycle.lastWindowEndMs)}
                    </span>
                  )}
                </>
              )}
            </div>
          </section>

          {/* The 5-layer strip (CLAUDE.md layer map) */}
          <section style={{ marginBottom: 30 }}>
            <SectionLabel count={v.layers.length}>Layers</SectionLabel>
            <div className="card" style={{ overflow: "hidden" }}>
              {v.layers.map((l) => (
                <div
                  key={l.key}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "28px minmax(0,1fr) 100px",
                    gap: 12,
                    alignItems: "center",
                    padding: "11px 16px",
                    borderBottom: "1px solid var(--border)",
                  }}
                >
                  <span className="mono muted" style={{ fontSize: 12 }}>L{l.n}</span>
                  <span style={{ fontSize: 13 }}>{l.name}</span>
                  <span style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12.5, justifyContent: "flex-end" }}>
                    <StatusDot color={layerColor(l.status)} live={l.status === "active" || l.status === "up"} />
                    <span className={l.status === "unknown" ? "muted" : "secondary"}>{l.status}</span>
                  </span>
                </div>
              ))}
            </div>
            <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
              L3/L5 report <span className="mono">unknown</span> — no per-layer probe yet; a real health rollup is a later build.
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function Stat({ label, value, color, live }: { label: string; value: number; color?: string; live?: boolean }) {
  return (
    <div className="card" style={{ padding: "14px 18px", minWidth: 120, flex: "1 1 120px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        {color && <StatusDot color={color} live={live} />}
        <span style={{ fontSize: 26, fontWeight: 650, letterSpacing: "-0.02em" }}>{value}</span>
      </div>
      <div className="eyebrow" style={{ marginTop: 4 }}>{label}</div>
    </div>
  );
}
