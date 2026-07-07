import { useEffect, useRef, useState } from "react";
import {
  fetchControl,
  pauseDispatch,
  resumeDispatch,
  setSchedule,
  type ControlState,
  type RunSchedule,
} from "../api";
import { mono, palettes } from "../theme";

// Global dispatch controls: a manual pause toggle + a daily run-window. Lives in
// the console header as a compact status pill that opens an inline panel. Mirrors
// the backend gate (server/http.py /control/*) — hold wins over schedule wins over
// nothing; the quota pause is shown read-only (it's set automatically).

const p = palettes.dark;

function statusOf(c: ControlState | null): { label: string; color: string } {
  if (!c) return { label: "…", color: p.textMuted };
  if (c.operatorHold.on) return { label: "Paused", color: p.amber };
  if (c.blocked && c.schedule.enabled) return { label: "Off-hours", color: p.amber };
  if (c.blocked) return { label: "Quota-held", color: p.amber };
  return { label: "Running", color: p.green };
}

export function DispatchControls() {
  const [ctrl, setCtrl] = useState<ControlState | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Local draft of the schedule form; synced from server state on load/refresh.
  const [draft, setDraft] = useState<RunSchedule | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  const refresh = () =>
    fetchControl()
      .then((c) => {
        setCtrl(c);
        setDraft(c.schedule);
        setErr(null);
      })
      .catch((e) => setErr(String(e)));

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000); // keep the pill live (schedule flips it)
    return () => clearInterval(t);
  }, []);

  // Close the panel on outside click.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const status = statusOf(ctrl);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div ref={rootRef} style={{ position: "relative" }}>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 7,
          height: 28,
          padding: "0 12px",
          borderRadius: 6,
          border: `1px solid ${p.borderStrong}`,
          background: open ? p.rowHover : "transparent",
          color: p.textPrimary,
          fontFamily: mono,
          fontSize: 12,
          cursor: "pointer",
        }}
      >
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: status.color,
            flexShrink: 0,
          }}
        />
        Dispatch: {status.label}
      </button>

      {open && (
        <div
          style={{
            position: "absolute",
            top: 34,
            right: 0,
            width: 320,
            padding: 16,
            borderRadius: 8,
            border: `1px solid ${p.borderStrong}`,
            background: p.bg,
            boxShadow: "0 8px 28px rgba(0,0,0,0.5)",
            zIndex: 50,
            display: "flex",
            flexDirection: "column",
            gap: 14,
          }}
        >
          {/* Manual hold */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <Label>Manual pause</Label>
            <div style={{ fontSize: 12, color: p.textSecondary }}>
              Stops all new goal dispatch. In-flight tasks finish.
            </div>
            {ctrl?.operatorHold.on ? (
              <button
                disabled={busy}
                onClick={() => run(() => resumeDispatch())}
                style={btn(p.green)}
              >
                Resume dispatch
              </button>
            ) : (
              <button
                disabled={busy}
                onClick={() => run(() => pauseDispatch("paused from console"))}
                style={btn(p.red)}
              >
                Pause now
              </button>
            )}
          </div>

          <div style={{ height: 1, background: p.border }} />

          {/* Daily run window */}
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <Label>Daily run window</Label>
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                fontSize: 12.5,
                color: p.textPrimary,
                cursor: "pointer",
              }}
            >
              <input
                type="checkbox"
                checked={draft?.enabled ?? false}
                onChange={(e) =>
                  setDraft((d) => (d ? { ...d, enabled: e.target.checked } : d))
                }
              />
              Only dispatch inside a time window
            </label>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <TimeField
                value={draft?.start ?? "09:00"}
                onChange={(v) => setDraft((d) => (d ? { ...d, start: v } : d))}
                disabled={!draft?.enabled}
              />
              <span style={{ color: p.textMuted, fontSize: 12 }}>to</span>
              <TimeField
                value={draft?.end ?? "18:00"}
                onChange={(v) => setDraft((d) => (d ? { ...d, end: v } : d))}
                disabled={!draft?.enabled}
              />
            </div>
            <input
              type="text"
              value={draft?.tz ?? ""}
              spellCheck={false}
              onChange={(e) => setDraft((d) => (d ? { ...d, tz: e.target.value } : d))}
              disabled={!draft?.enabled}
              placeholder="Europe/Kyiv"
              style={{
                height: 28,
                padding: "0 10px",
                borderRadius: 6,
                border: `1px solid ${p.border}`,
                background: p.rowHover,
                color: p.textPrimary,
                fontFamily: mono,
                fontSize: 12,
                opacity: draft?.enabled ? 1 : 0.5,
              }}
            />
            <button
              disabled={busy || !draft}
              onClick={() => draft && run(() => setSchedule(draft))}
              style={btn(p.accent)}
            >
              Save window
            </button>
          </div>

          {ctrl?.blocked && ctrl.reason && (
            <div style={{ fontSize: 11.5, color: p.amber, fontFamily: mono }}>
              Held: {ctrl.reason}
            </div>
          )}
          {err && (
            <div style={{ fontSize: 11.5, color: p.red, fontFamily: mono }}>{err}</div>
          )}
        </div>
      )}
    </div>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: "0.06em",
        textTransform: "uppercase",
        color: p.textSecondary,
      }}
    >
      {children}
    </div>
  );
}

function TimeField({
  value,
  onChange,
  disabled,
}: {
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
}) {
  return (
    <input
      type="time"
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      style={{
        height: 28,
        padding: "0 8px",
        borderRadius: 6,
        border: `1px solid ${p.border}`,
        background: p.rowHover,
        color: p.textPrimary,
        fontFamily: mono,
        fontSize: 12,
        opacity: disabled ? 0.5 : 1,
      }}
    />
  );
}

function btn(color: string): React.CSSProperties {
  return {
    height: 30,
    borderRadius: 6,
    border: `1px solid ${color}`,
    background: "transparent",
    color,
    fontFamily: mono,
    fontSize: 12,
    fontWeight: 600,
    cursor: "pointer",
  };
}
