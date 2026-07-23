import { useEffect, useState } from "react";
import { fetchTraces, type TraceEvent } from "../api";
import { relativeTime } from "../util/time";
import { EmptyState, ErrorNote, Loading, StatusDot, TieredDisclosure } from "../ui";

// LayerTrace — the cross-cutting layer trace (ADR 0008 P1, PR-E). One tick
// (trace_id) rendered hop-by-hop across the 5 layers, backed by /traces.json.
// A hop that errored is highlighted IN PLACE (red rail + the error text) rather
// than hidden, so a TransitionConflict / gate crash / timeout shows exactly
// where in the stack it happened. Recent ticks show; older fold.

const LAYER: Record<string, { n: number; name: string }> = {
  notify: { n: 2, name: "GoalService" },
  cognition: { n: 3, name: "Cognition" },
  dispatch: { n: 4, name: "Engine" },
  delivery: { n: 4, name: "Delivery" },
  subprocess: { n: 5, name: "Worker" },
};
function layerOf(kind: string): { n: number; name: string } {
  return LAYER[kind] ?? { n: 0, name: kind };
}

function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v : null;
}

interface Tick {
  traceId: string;
  hops: TraceEvent[]; // chronological
  latestTs: number;
  hasError: boolean;
}

function toTicks(events: TraceEvent[]): Tick[] {
  const byTrace = new Map<string, TraceEvent[]>();
  for (const e of events) {
    const arr = byTrace.get(e.trace_id) ?? [];
    arr.push(e);
    byTrace.set(e.trace_id, arr);
  }
  const ticks: Tick[] = [];
  for (const [traceId, hops] of byTrace) {
    hops.sort((a, b) => a.ts - b.ts);
    ticks.push({
      traceId,
      hops,
      latestTs: Math.max(...hops.map((h) => h.ts)),
      hasError: hops.some((h) => str(h.payload.error)),
    });
  }
  return ticks.sort((a, b) => b.latestTs - a.latestTs);
}

const RECENT = 8;

export function LayerTrace({ goalId }: { goalId: string }) {
  const [events, setEvents] = useState<TraceEvent[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => fetchTraces(goalId).then((r) => alive && setEvents(r)).catch((e) => alive && setErr(String(e)));
    load();
    const t = setInterval(load, 20000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [goalId]);

  if (err) return <ErrorNote>{err}</ErrorNote>;
  if (!events) return <Loading />;
  if (events.length === 0) return <EmptyState title="No trace yet" hint="Ticks land here once the goal starts planning and dispatching." />;

  const ticks = toTicks(events);
  const recent = ticks.slice(0, RECENT);
  const older = ticks.slice(RECENT);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {recent.map((t) => <TickBlock key={t.traceId} t={t} />)}
      {older.length > 0 && (
        <TieredDisclosure label="Older ticks" count={older.length}>
          <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 6 }}>
            {older.map((t) => <TickBlock key={t.traceId} t={t} />)}
          </div>
        </TieredDisclosure>
      )}
    </div>
  );
}

function TickBlock({ t }: { t: Tick }) {
  return (
    <div className="card" style={{ overflow: "hidden" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px 16px", borderBottom: "1px solid var(--border)" }}>
        <StatusDot color={t.hasError ? "var(--red)" : "var(--green)"} />
        <span className="mono truncate muted" style={{ fontSize: 11.5 }} title={t.traceId}>tick {t.traceId.slice(0, 8)}</span>
        <span className="mono muted" style={{ fontSize: 11 }}>{t.hops.length} hop{t.hops.length === 1 ? "" : "s"}</span>
        <span className="mono secondary" style={{ marginLeft: "auto", fontSize: 11 }}>{relativeTime(t.latestTs)}</span>
      </div>
      {t.hops.map((h) => <Hop key={h.id} h={h} />)}
    </div>
  );
}

function Hop({ h }: { h: TraceEvent }) {
  const layer = layerOf(h.kind);
  const role = str(h.payload.role);
  const error = str(h.payload.error);
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "34px 96px minmax(0,1fr)",
        gap: 10,
        alignItems: "baseline",
        padding: "9px 16px",
        borderBottom: "1px solid var(--border)",
        borderLeft: error ? "2px solid var(--red)" : "2px solid transparent",
        background: error ? "var(--red-soft)" : undefined,
      }}
    >
      <span className="mono muted" style={{ fontSize: 11.5 }}>{layer.n ? `L${layer.n}` : "—"}</span>
      <span className="secondary" style={{ fontSize: 12 }}>{layer.name}</span>
      <span style={{ fontSize: 12 }}>
        <span className="mono secondary">{h.kind}</span>
        {role && <span className="muted"> · {role}</span>}
        {error && (
          <div className="mono" style={{ color: "var(--red)", fontSize: 11.5, marginTop: 3, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
            {error}
          </div>
        )}
      </span>
    </div>
  );
}
