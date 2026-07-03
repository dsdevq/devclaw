import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { fetchGoal, type GoalDetail as GD, type Verdict } from "../api";
import { EventFeed } from "../components/EventFeed";
import { mono, palettes } from "../theme";

// PR#3 delivers the static frame of Goal Detail.dc.html: header, breadcrumb,
// objective, phase/lifecycle/verdict pills, Cancel + Steer buttons (visual
// only), and the 5-node phase timeline with pulse on the current node.
// The live event stream + kind filters + row expand land in PR#4-5.

const VERSION_LABEL = "v0.7.2";

const VERDICT_LABEL: Record<Verdict, string> = {
  on_track: "On track",
  off_track: "Off track",
  achieved: "Achieved",
  stalled: "Stalled",
  needs_human: "Needs human",
};

const pulseKeyframes = `
@keyframes devclaw-pulse {
  0%   { box-shadow: 0 0 0 0 rgba(94,106,210,0.55); }
  70%  { box-shadow: 0 0 0 7px rgba(94,106,210,0); }
  100% { box-shadow: 0 0 0 0 rgba(94,106,210,0); }
}
`;

export function GoalDetail() {
  const { id } = useParams<{ id: string }>();
  const p = palettes.dark;
  const [data, setData] = useState<GD | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    let alive = true;
    setData(null);
    setErr(null);
    fetchGoal(id)
      .then((d) => alive && setData(d))
      .catch((e) => alive && setErr(String(e)));
    return () => {
      alive = false;
    };
  }, [id]);

  const verdictMeta = useMemo(() => {
    if (!data?.direction) {
      return { label: "—", color: p.textMuted };
    }
    const v = data.direction.verdict;
    const label = VERDICT_LABEL[v] ?? v;
    const color =
      v === "achieved" || v === "on_track"
        ? p.green
        : v === "off_track" || v === "stalled"
          ? p.amber
          : v === "needs_human"
            ? p.red
            : p.textMuted;
    return { label, color };
  }, [data, p]);

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
      <style>{pulseKeyframes}</style>

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
              flexShrink: 0,
              padding: "22px 40px 20px",
              boxSizing: "border-box",
              borderBottom: `1px solid ${p.border}`,
              display: "flex",
              flexDirection: "column",
              gap: 14,
            }}
          >
            <div
              style={{
                fontSize: 15,
                lineHeight: 1.5,
                color: p.textPrimary,
                maxWidth: 900,
              }}
            >
              {data.objective || "—"}
            </div>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 20,
                flexWrap: "wrap",
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  flexWrap: "wrap",
                }}
              >
                <Pill
                  labelColor={p.textMuted}
                  border={p.border}
                  label="Phase"
                  value={data.phaseLabel}
                  dotColor={p.accent}
                />
                <Pill
                  labelColor={p.textMuted}
                  border={p.border}
                  label="Lifecycle"
                  value={data.lifecycle ?? "—"}
                />
                <Pill
                  labelColor={p.textMuted}
                  border={p.border}
                  label="Verdict"
                  value={verdictMeta.label}
                  dotColor={verdictMeta.color}
                />
              </div>
              <div
                style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}
              >
                {/* Cancel + Steer are visual-only in PR#3; wiring lands in PR#6. */}
                <button
                  disabled
                  style={{
                    background: "transparent",
                    color: p.red,
                    border: `1px solid ${p.red}`,
                    padding: "8px 18px",
                    borderRadius: 5,
                    fontSize: 13,
                    fontWeight: 600,
                    cursor: "not-allowed",
                    opacity: 0.6,
                  }}
                >
                  Cancel
                </button>
                <button
                  disabled
                  style={{
                    background: p.accent,
                    color: "#ffffff",
                    border: "none",
                    padding: "8px 18px",
                    borderRadius: 5,
                    fontSize: 13,
                    fontWeight: 600,
                    cursor: "not-allowed",
                    opacity: 0.6,
                  }}
                >
                  Steer
                </button>
              </div>
            </div>
          </div>

          <PhaseTimeline nodes={data.timeline} palette={p} />

          <EventFeed goalId={data.id} />
        </>
      )}
    </div>
  );
}

function Pill({
  label,
  value,
  labelColor,
  border,
  dotColor,
}: {
  label: string;
  value: string;
  labelColor: string;
  border: string;
  dotColor?: string;
}) {
  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 7,
        padding: "5px 10px",
        border: `1px solid ${border}`,
        borderRadius: 5,
        fontSize: 12,
      }}
    >
      {dotColor && (
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: dotColor,
            flexShrink: 0,
          }}
        />
      )}
      <span
        style={{
          color: labelColor,
          textTransform: "uppercase",
          fontSize: 10,
          letterSpacing: "0.04em",
        }}
      >
        {label}
      </span>
      <span>{value}</span>
    </div>
  );
}

function PhaseTimeline({
  nodes,
  palette: p,
}: {
  nodes: GD["timeline"];
  palette: (typeof palettes)["dark"];
}) {
  const currentIndex = nodes.findIndex((n) => n.current);
  const lineFraction =
    nodes.length > 1
      ? ((currentIndex === -1 ? 0 : currentIndex) / (nodes.length - 1)) * 100
      : 0;
  return (
    <div
      style={{
        flexShrink: 0,
        padding: "16px 40px",
        boxSizing: "border-box",
        borderBottom: `1px solid ${p.border}`,
      }}
    >
      <div style={{ position: "relative", height: 4, margin: "0 8px" }}>
        <div
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            height: 2,
            background: `linear-gradient(to right, ${p.accent} 0%, ${p.accent} ${lineFraction}%, ${p.border} ${lineFraction}%, ${p.border} 100%)`,
          }}
        />
        <div
          style={{
            position: "absolute",
            top: -6,
            left: 0,
            right: 0,
            display: "flex",
            justifyContent: "space-between",
          }}
        >
          {nodes.map((n) => {
            let bg = p.bg;
            let border = p.border;
            let extra: React.CSSProperties = {};
            if (n.current) {
              bg = p.accent;
              border = p.accent;
              extra = { animation: "devclaw-pulse 2s infinite" };
            } else if (n.reached) {
              bg = p.accent;
              border = p.accent;
              extra = { opacity: 0.5 };
            }
            const labelColor = n.current
              ? p.textPrimary
              : n.reached
                ? p.textSecondary
                : p.textMuted;
            return (
              <div
                key={n.name}
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: 5,
                  width: 70,
                }}
              >
                <span
                  style={{
                    display: "block",
                    width: 10,
                    height: 10,
                    borderRadius: "50%",
                    background: bg,
                    border: `2px solid ${border}`,
                    boxSizing: "border-box",
                    ...extra,
                  }}
                />
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: n.current ? 600 : 500,
                    color: labelColor,
                  }}
                >
                  {n.name}
                </span>
                <span
                  style={{
                    fontFamily: mono,
                    fontSize: 9.5,
                    color: p.textMuted,
                  }}
                >
                  —
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
