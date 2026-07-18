import { useEffect, useMemo, useRef, useState } from "react";
import { EVENT_KINDS, goalEventsUrl, type EventKind, type StreamEvent } from "../api";

// Live SSE event stream for a goal: kind filter chips, expandable rows, an
// autoscroll toggle, and a pending stripe when autoscroll is off. The stream
// delivers the whole bounded history at connect and reconnects on "done".

const MAX_EVENTS = 500;

export function EventFeed({ goalId }: { goalId: string }) {
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [pending, setPending] = useState<StreamEvent[]>([]);
  const [autoScroll, setAutoScroll] = useState(true);
  const autoRef = useRef(autoScroll);
  autoRef.current = autoScroll;
  const feedRef = useRef<HTMLDivElement>(null);
  const [kinds, setKinds] = useState<Record<EventKind, boolean>>(
    () => Object.fromEntries(EVENT_KINDS.map((k) => [k, true])) as Record<EventKind, boolean>,
  );
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  useEffect(() => {
    setEvents([]);
    setPending([]);
    setExpanded({});
    let closed = false;
    let es: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (closed) return;
      es = new EventSource(goalEventsUrl(goalId));
      es.onmessage = (m) => {
        try {
          const ev = JSON.parse(m.data) as StreamEvent;
          if (autoRef.current) {
            setEvents((prev) => [ev, ...prev].slice(0, MAX_EVENTS));
            requestAnimationFrame(() => feedRef.current && (feedRef.current.scrollTop = 0));
          } else {
            setPending((prev) => [ev, ...prev]);
          }
        } catch {
          /* malformed frame */
        }
      };
      es.addEventListener("done", () => {
        es?.close();
        if (!closed) timer = setTimeout(connect, 1500);
      });
    };
    connect();
    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      es?.close();
    };
  }, [goalId]);

  const drain = () => {
    setEvents((prev) => [...pending, ...prev].slice(0, MAX_EVENTS));
    setPending([]);
    requestAnimationFrame(() => feedRef.current && (feedRef.current.scrollTop = 0));
  };

  const toggleAuto = () =>
    setAutoScroll((prev) => {
      const next = !prev;
      if (next && pending.length) drain();
      return next;
    });

  const rows = useMemo(() => events.filter((e) => kinds[e.kind]), [events, kinds]);

  return (
    <div className="card" style={{ display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 10,
          padding: "10px 14px",
          borderBottom: "1px solid var(--border)",
          flexWrap: "wrap",
        }}
      >
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {EVENT_KINDS.map((k) => (
            <button
              key={k}
              onClick={() => setKinds((s) => ({ ...s, [k]: !s[k] }))}
              className="mono"
              style={{
                fontSize: 10.5,
                textTransform: "uppercase",
                letterSpacing: "0.03em",
                padding: "4px 9px",
                borderRadius: 999,
                cursor: "pointer",
                border: `1px solid ${kinds[k] ? "var(--border-strong)" : "var(--border)"}`,
                background: kinds[k] ? "var(--row-hover)" : "transparent",
                color: kinds[k] ? "var(--text)" : "var(--text-muted)",
              }}
            >
              {k}
            </button>
          ))}
        </div>
        <button className="btn ghost sm" onClick={toggleAuto}>
          Auto-scroll {autoScroll ? "on" : "off"}
        </button>
      </div>

      <div ref={feedRef} style={{ maxHeight: 460, overflowY: "auto", position: "relative" }}>
        {pending.length > 0 && (
          <div
            onClick={drain}
            style={{ position: "sticky", top: 0, zIndex: 2, display: "flex", justifyContent: "center", padding: "8px 0", cursor: "pointer" }}
          >
            <span className="btn primary sm">{pending.length} new event{pending.length === 1 ? "" : "s"}</span>
          </div>
        )}
        {rows.length === 0 && (
          <div className="mono muted" style={{ padding: "20px 14px", fontSize: 12 }}>waiting for events…</div>
        )}
        {rows.map((ev) => (
          <EventRow
            key={ev.id}
            event={ev}
            expanded={!!expanded[String(ev.id)]}
            onToggle={() => setExpanded((s) => ({ ...s, [String(ev.id)]: !s[String(ev.id)] }))}
          />
        ))}
      </div>
    </div>
  );
}

function truncateMiddle(s: string, max: number): string {
  if (s.length <= max) return s;
  const half = Math.floor((max - 1) / 2);
  return s.slice(0, half) + "…" + s.slice(s.length - half);
}

function relativeMs(ms: number): string {
  const diff = Date.now() - ms;
  if (diff < 0) return "just now";
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h ago`;
}

function EventRow({ event: e, expanded, onToggle }: { event: StreamEvent; expanded: boolean; onToggle: () => void }) {
  const payload = (e.payload ?? {}) as Record<string, unknown>;
  let main = e.type;
  let preview = "";
  let previewColor = "var(--text-secondary)";
  const details: { k: string; v: string }[] = [];
  const add = (keys: string[]) => {
    for (const k of keys) if (payload[k] !== undefined) details.push({ k, v: String(payload[k]) });
  };

  if (e.kind === "cognition") {
    const role = String(payload.role ?? e.source ?? "");
    const hash = String(payload.prompt_hash ?? payload.hash ?? "").slice(0, 6);
    const latency = payload.latency_ms ?? payload.latencyMs;
    main = [role, hash, latency && `${latency}ms`].filter(Boolean).join(" · ") || e.type;
    preview = String(payload.preview ?? payload.text ?? "");
    add(["role", "prompt_hash", "latency_ms", "tokens_in", "tokens_out", "response", "preview", "text"]);
  } else if (e.kind === "subprocess") {
    const cmd = String(payload.command ?? payload.cmd ?? payload.line ?? "");
    main = cmd ? `$ ${truncateMiddle(cmd, 54)}` : e.type;
    const exit = payload.exit_code ?? payload.exitCode;
    if (exit !== undefined) {
      preview = `exit ${exit}`;
      previewColor = exit === 0 ? "var(--green)" : "var(--red)";
    } else preview = String(payload.line ?? "");
    add(["command", "exit_code", "duration_ms", "cwd", "line"]);
  } else if (e.kind === "dispatch") {
    main = String(payload.task_tool ?? payload.tool ?? "") || e.type;
    preview = payload.task_id ? `task ${payload.task_id}` : "";
    add(["task_tool", "task_id", "params"]);
  } else if (e.kind === "delivery") {
    const pr = payload.pr_number ?? payload.prNumber;
    main = pr ? `PR #${pr}` : e.type;
    const verdict = String(payload.gate_verdict ?? payload.verdict ?? "");
    preview = verdict ? `gate: ${verdict}` : "";
    previewColor = verdict === "passed" ? "var(--green)" : verdict === "failed" ? "var(--red)" : "var(--amber)";
    add(["pr_url", "gate_verdict", "verdict", "report"]);
  } else {
    const level = String(payload.level ?? "info");
    main = level;
    preview = String(payload.text ?? payload.message ?? payload.reason ?? e.type);
    previewColor = level === "warn" ? "var(--amber)" : level === "critical" || level === "error" ? "var(--red)" : "var(--text-secondary)";
    add(["level", "text", "message", "reason", "source"]);
  }

  return (
    <div style={{ borderBottom: "1px solid var(--border)", background: expanded ? "var(--row-hover)" : "transparent" }}>
      <div
        onClick={onToggle}
        style={{
          display: "grid",
          gridTemplateColumns: "80px minmax(0,1fr) 78px",
          gap: 12,
          alignItems: "center",
          padding: "8px 14px",
          cursor: "pointer",
        }}
      >
        <span className="mono muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.03em" }}>{e.kind}</span>
        <div className="mono" style={{ display: "flex", gap: 9, overflow: "hidden", whiteSpace: "nowrap", fontSize: 12 }}>
          <span style={{ flexShrink: 0 }}>{main}</span>
          <span className="truncate" style={{ color: previewColor }}>{preview}</span>
        </div>
        <span className="mono muted" style={{ fontSize: 11, textAlign: "right" }}>
          {typeof e.ts === "number" ? relativeMs(e.ts) : String(e.ts)}
        </span>
      </div>
      {expanded && details.length > 0 && (
        <div style={{ padding: "4px 14px 12px 94px" }}>
          {details.map((d) => (
            <div key={d.k} className="mono" style={{ display: "flex", gap: 12, fontSize: 11.5, padding: "2px 0" }}>
              <span className="muted" style={{ width: 96, flexShrink: 0 }}>{d.k}</span>
              <span style={{ overflowWrap: "anywhere" }}>{d.v}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
