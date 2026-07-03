import { useEffect, useMemo, useRef, useState } from "react";
import {
  EVENT_KINDS,
  goalEventsUrl,
  type EventKind,
  type StreamEvent,
} from "../api";
import { mono, palettes } from "../theme";

// The event stream half of Goal Detail.dc.html.
// PR#4: kind filter chips + expandable rows + SSE live feed.
// PR#5 (this): autoscroll toggle + pending-merge pill for paused sessions.
// Note on infinite scroll — the design mock pages older events into memory,
// but our SSE contract delivers the whole history at connect (bounded by the
// state store) so there's no untelled backlog to page in. If we ever cap the
// server-side replay we'll add a paginated /events/history endpoint.

const MAX_EVENTS = 500; // console-side cap; older events fall off the tail

interface Props {
  goalId: string;
}

export function EventFeed({ goalId }: Props) {
  const p = palettes.dark;
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [pending, setPending] = useState<StreamEvent[]>([]);
  const [autoScroll, setAutoScroll] = useState<boolean>(true);
  const autoScrollRef = useRef(autoScroll);
  autoScrollRef.current = autoScroll;
  const feedRef = useRef<HTMLDivElement>(null);
  const [selectedKinds, setSelectedKinds] = useState<Record<EventKind, boolean>>(
    () => Object.fromEntries(EVENT_KINDS.map((k) => [k, true])) as Record<
      EventKind,
      boolean
    >,
  );
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  useEffect(() => {
    setEvents([]);
    setPending([]);
    setExpanded({});
    let closed = false;
    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (closed) return;
      es = new EventSource(goalEventsUrl(goalId));
      es.onmessage = (m) => {
        try {
          const ev = JSON.parse(m.data) as StreamEvent;
          if (autoScrollRef.current) {
            setEvents((prev) => {
              const next = [ev, ...prev];
              return next.length > MAX_EVENTS ? next.slice(0, MAX_EVENTS) : next;
            });
            // Snap feed to top on next paint so the newest event is visible.
            requestAnimationFrame(() => {
              if (feedRef.current) feedRef.current.scrollTop = 0;
            });
          } else {
            // Paused: queue events into the pending stripe. Merge is user-driven
            // via the "N new events" pill.
            setPending((prev) => [ev, ...prev]);
          }
        } catch {
          /* malformed frame — ignore */
        }
      };
      es.addEventListener("done", () => {
        es?.close();
        if (!closed) {
          reconnectTimer = setTimeout(connect, 1500);
        }
      });
      es.onerror = () => {
        // Browser retries transient errors automatically.
      };
    };

    connect();
    return () => {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      es?.close();
    };
  }, [goalId]);

  const mergePending = () => {
    setEvents((prev) => {
      const next = [...pending, ...prev];
      return next.length > MAX_EVENTS ? next.slice(0, MAX_EVENTS) : next;
    });
    setPending([]);
    requestAnimationFrame(() => {
      if (feedRef.current) feedRef.current.scrollTop = 0;
    });
  };

  const toggleAutoScroll = () => {
    setAutoScroll((prev) => {
      const next = !prev;
      // Re-enabling autoscroll implicitly drains the pending stripe so the
      // user isn't left with a permanent pill they can't clear.
      if (next && pending.length > 0) {
        setEvents((old) => {
          const merged = [...pending, ...old];
          return merged.length > MAX_EVENTS ? merged.slice(0, MAX_EVENTS) : merged;
        });
        setPending([]);
      }
      return next;
    });
  };

  const chips = useMemo(
    () =>
      EVENT_KINDS.map((k) => ({
        kind: k,
        active: selectedKinds[k],
      })),
    [selectedKinds],
  );

  const rows = useMemo(
    () => events.filter((e) => selectedKinds[e.kind]),
    [events, selectedKinds],
  );

  const toggleKind = (k: EventKind) =>
    setSelectedKinds((s) => ({ ...s, [k]: !s[k] }));

  const toggleExpanded = (id: number) =>
    setExpanded((s) => ({ ...s, [String(id)]: !s[String(id)] }));

  return (
    <div
      style={{
        flex: 1,
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
        padding: "0 40px",
        boxSizing: "border-box",
      }}
    >
      <div
        style={{
          flexShrink: 0,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "14px 0 10px",
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
          Event stream
        </span>
        <div
          onClick={toggleAutoScroll}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            cursor: "pointer",
            userSelect: "none",
          }}
        >
          <span style={{ fontSize: 12, color: p.textSecondary }}>Auto-scroll</span>
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              width: 32,
              height: 18,
              borderRadius: 9,
              background: autoScroll ? p.accent : p.rowHover,
              border: `1px solid ${autoScroll ? p.accent : p.border}`,
              padding: 2,
              boxSizing: "border-box",
              transition: "background 0.12s ease",
            }}
          >
            <span
              style={{
                display: "block",
                width: 12,
                height: 12,
                borderRadius: "50%",
                background: "#ffffff",
                transform: `translateX(${autoScroll ? 14 : 0}px)`,
                transition: "transform 0.12s ease",
              }}
            />
          </span>
        </div>
      </div>

      <div
        style={{
          flexShrink: 0,
          display: "flex",
          alignItems: "center",
          gap: 8,
          flexWrap: "wrap",
          paddingBottom: 10,
        }}
      >
        {chips.map((c) => (
          <div
            key={c.kind}
            onClick={() => toggleKind(c.kind)}
            style={{
              fontFamily: mono,
              fontSize: 11,
              textTransform: "uppercase",
              letterSpacing: "0.03em",
              padding: "5px 11px",
              borderRadius: 12,
              cursor: "pointer",
              userSelect: "none",
              border: `1px solid ${
                c.active ? p.textSecondary : p.border
              }`,
              color: c.active ? p.textPrimary : p.textMuted,
              background: c.active ? p.rowHover : "transparent",
            }}
          >
            {c.kind}
          </div>
        ))}
      </div>

      <div
        ref={feedRef}
        style={{
          flex: 1,
          minHeight: 0,
          overflowY: "auto",
          borderTop: `1px solid ${p.border}`,
        }}
      >
        {pending.length > 0 && (
          <div
            onClick={mergePending}
            style={{
              position: "sticky",
              top: 0,
              zIndex: 2,
              display: "flex",
              justifyContent: "center",
              padding: "8px 0",
              cursor: "pointer",
              background: `linear-gradient(to bottom, ${p.bg} 0%, ${p.bg} 60%, transparent 100%)`,
            }}
          >
            <span
              style={{
                background: p.accent,
                color: "#ffffff",
                fontSize: 11.5,
                fontWeight: 600,
                padding: "5px 13px",
                borderRadius: 12,
              }}
            >
              {pending.length} new event{pending.length === 1 ? "" : "s"}
            </span>
          </div>
        )}
        {rows.length === 0 && (
          <div
            style={{
              padding: "22px 6px",
              fontFamily: mono,
              fontSize: 12,
              color: p.textMuted,
            }}
          >
            waiting for events…
          </div>
        )}
        {rows.map((ev) => (
          <EventRow
            key={ev.id}
            event={ev}
            expanded={!!expanded[String(ev.id)]}
            onToggle={() => toggleExpanded(ev.id)}
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

function EventRow({
  event: e,
  expanded,
  onToggle,
}: {
  event: StreamEvent;
  expanded: boolean;
  onToggle: () => void;
}) {
  const p = palettes.dark;
  const payload = (e.payload ?? {}) as Record<string, unknown>;

  let main = e.type;
  let preview = "";
  let previewColor = p.textSecondary;
  const details: { k: string; v: string }[] = [];

  if (e.kind === "cognition") {
    const role = String(payload.role ?? e.source ?? "");
    const hash = String(payload.prompt_hash ?? payload.hash ?? "").slice(0, 6);
    const latency = payload.latency_ms ?? payload.latencyMs;
    const tokIn = payload.tokens_in ?? payload.tokIn;
    const tokOut = payload.tokens_out ?? payload.tokOut;
    const parts = [role, hash, latency && `${latency}ms`, tokIn && tokOut && `${tokIn}→${tokOut} tok`]
      .filter(Boolean)
      .join(" · ");
    main = parts || e.type;
    preview = String(payload.preview ?? payload.text ?? "");
    for (const k of ["role", "prompt_hash", "hash", "latency_ms", "tokens_in", "tokens_out", "response", "preview", "text"]) {
      if (payload[k] !== undefined) details.push({ k, v: String(payload[k]) });
    }
  } else if (e.kind === "subprocess") {
    const cmd = String(payload.command ?? payload.cmd ?? payload.line ?? "");
    main = cmd ? `$ ${truncateMiddle(cmd, 56)}` : e.type;
    const exit = payload.exit_code ?? payload.exitCode;
    if (exit !== undefined) {
      preview = `exit ${exit}`;
      previewColor = exit === 0 ? p.green : p.red;
    } else {
      preview = String(payload.line ?? "");
    }
    for (const k of ["command", "exit_code", "duration_ms", "cwd", "line"]) {
      if (payload[k] !== undefined) details.push({ k, v: String(payload[k]) });
    }
  } else if (e.kind === "dispatch") {
    const tool = String(payload.task_tool ?? payload.tool ?? "");
    main = tool || e.type;
    preview = payload.task_id ? `task ${payload.task_id}` : "";
    for (const k of ["task_tool", "tool", "task_id", "params"]) {
      if (payload[k] !== undefined) details.push({ k, v: String(payload[k]) });
    }
  } else if (e.kind === "delivery") {
    const pr = payload.pr_number ?? payload.prNumber;
    main = pr ? `PR #${pr}` : e.type;
    const verdict = String(payload.gate_verdict ?? payload.verdict ?? "");
    preview = verdict ? `gate: ${verdict}` : "";
    previewColor =
      verdict === "passed"
        ? p.green
        : verdict === "failed"
          ? p.red
          : p.amber;
    for (const k of ["pr_url", "prUrl", "gate_verdict", "verdict", "report"]) {
      if (payload[k] !== undefined) details.push({ k, v: String(payload[k]) });
    }
  } else {
    const level = String(payload.level ?? "info");
    main = level;
    preview = String(payload.text ?? payload.message ?? payload.reason ?? e.type);
    previewColor =
      level === "warn" ? p.amber : level === "critical" || level === "error" ? p.red : p.textSecondary;
    for (const k of ["level", "text", "message", "reason", "source"]) {
      if (payload[k] !== undefined) details.push({ k, v: String(payload[k]) });
    }
  }

  return (
    <div
      style={{
        borderBottom: `1px solid ${p.border}`,
        background: expanded ? p.rowHover : "transparent",
      }}
    >
      <div
        onClick={onToggle}
        style={{
          display: "grid",
          gridTemplateColumns: "14px 90px minmax(0,1fr) 100px",
          gap: 14,
          alignItems: "center",
          minHeight: 34,
          padding: "7px 4px",
          cursor: "pointer",
        }}
      >
        <span
          style={{
            display: "inline-block",
            width: 0,
            height: 0,
            flexShrink: 0,
            borderStyle: "solid",
            borderWidth: "3.5px 0 3.5px 5px",
            borderColor: `transparent transparent transparent ${p.textMuted}`,
            transition: "transform 0.12s ease",
            transform: `rotate(${expanded ? 90 : 0}deg)`,
          }}
        />
        <span
          style={{
            fontFamily: mono,
            fontSize: 10.5,
            letterSpacing: "0.03em",
            textTransform: "uppercase",
            color: p.textMuted,
          }}
        >
          {e.kind}
        </span>
        <div
          style={{
            display: "flex",
            gap: 9,
            overflow: "hidden",
            whiteSpace: "nowrap",
            fontFamily: mono,
            fontSize: 12.5,
          }}
        >
          <span style={{ color: p.textPrimary, flexShrink: 0 }}>{main}</span>
          <span
            style={{
              color: previewColor,
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {preview}
          </span>
        </div>
        <span
          title={String(e.ts)}
          style={{
            fontFamily: mono,
            fontSize: 11.5,
            textAlign: "right",
            color: p.textMuted,
          }}
        >
          {typeof e.ts === "number" ? relativeMs(e.ts) : String(e.ts)}
        </span>
      </div>
      {expanded && details.length > 0 && (
        <div
          style={{
            margin: "0 0 10px 106px",
            padding: "10px 14px",
            background: p.rowHover,
            borderRadius: 4,
          }}
        >
          {details.map((d) => (
            <div
              key={d.k}
              style={{
                display: "flex",
                gap: 12,
                fontFamily: mono,
                fontSize: 11.5,
                padding: "2px 0",
              }}
            >
              <span style={{ width: 100, flexShrink: 0, color: p.textMuted }}>
                {d.k}
              </span>
              <span style={{ color: p.textPrimary, overflowWrap: "anywhere" }}>
                {d.v}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function relativeMs(ms: number): string {
  const diff = Date.now() - ms;
  if (diff < 0) return "just now";
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ago`;
}
