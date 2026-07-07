import { useEffect, useState } from "react";
import { fetchGoalSchedule, setGoalSchedule, type RunSchedule } from "../api";
import { mono, palettes } from "../theme";

// Per-goal run window: a night/off-hours narrowing on top of the GLOBAL dispatch
// window (DispatchControls in the header). A goal dispatches only if BOTH the
// global controls AND its own window allow — so a token-heavy standing goal can
// be confined to nights while the rest of the engine runs all day. Backend gate:
// server/http.py GET/POST /goals/{id}/schedule → tick_all's per-goal loop.
// Enabled ⇒ the goal only ticks inside [start, end); outside it's held (0 tokens,
// in-flight finishes). Disabled ⇒ the goal follows the global window only.

const p = palettes.dark;

export function GoalRunWindow({ goalId }: { goalId: string }) {
  const [draft, setDraft] = useState<RunSchedule | null>(null);
  const [saved, setSaved] = useState<RunSchedule | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    fetchGoalSchedule(goalId)
      .then((s) => {
        if (!live) return;
        setDraft(s);
        setSaved(s);
        setErr(null);
      })
      .catch((e) => live && setErr(String(e instanceof Error ? e.message : e)));
    return () => {
      live = false;
    };
  }, [goalId]);

  const dirty =
    !!draft &&
    !!saved &&
    (draft.enabled !== saved.enabled ||
      draft.start !== saved.start ||
      draft.end !== saved.end ||
      draft.tz !== saved.tz);

  const save = async () => {
    if (!draft) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await setGoalSchedule(goalId, draft);
      setSaved(r.schedule);
      setDraft(r.schedule);
      setFlash(r.schedule.enabled ? "Window saved" : "Window disabled");
      setTimeout(() => setFlash(null), 2500);
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  const summary = saved?.enabled
    ? `Nights only · ${saved.start}–${saved.end} ${saved.tz}`
    : "Follows the global window";

  return (
    <section style={{ padding: "18px 40px", borderTop: `1px solid ${p.border}` }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 12,
        }}
      >
        <div
          style={{
            fontSize: 11,
            fontWeight: 600,
            letterSpacing: "0.06em",
            textTransform: "uppercase",
            color: p.textSecondary,
          }}
        >
          Run window
        </div>
        <div style={{ fontFamily: mono, fontSize: 11.5, color: p.textMuted }}>{summary}</div>
      </div>

      {!draft && !err && (
        <div style={{ fontSize: 12.5, color: p.textMuted }}>Loading…</div>
      )}

      {draft && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 14,
            flexWrap: "wrap",
          }}
        >
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
              checked={draft.enabled}
              onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}
            />
            Only dispatch inside a time window
          </label>

          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <TimeField
              value={draft.start}
              disabled={!draft.enabled}
              onChange={(v) => setDraft({ ...draft, start: v })}
            />
            <span style={{ color: p.textMuted, fontSize: 12 }}>to</span>
            <TimeField
              value={draft.end}
              disabled={!draft.enabled}
              onChange={(v) => setDraft({ ...draft, end: v })}
            />
          </div>

          <input
            type="text"
            value={draft.tz}
            spellCheck={false}
            disabled={!draft.enabled}
            placeholder="Europe/Kyiv"
            onChange={(e) => setDraft({ ...draft, tz: e.target.value })}
            style={{
              height: 28,
              width: 150,
              padding: "0 10px",
              borderRadius: 6,
              border: `1px solid ${p.border}`,
              background: p.rowHover,
              color: p.textPrimary,
              fontFamily: mono,
              fontSize: 12,
              opacity: draft.enabled ? 1 : 0.5,
            }}
          />

          <button
            disabled={busy || !dirty}
            onClick={save}
            style={{
              height: 30,
              padding: "0 16px",
              borderRadius: 6,
              border: `1px solid ${p.accent}`,
              background: dirty ? p.accent : "transparent",
              color: dirty ? "#ffffff" : p.accent,
              fontFamily: mono,
              fontSize: 12,
              fontWeight: 600,
              cursor: busy || !dirty ? "not-allowed" : "pointer",
              opacity: busy || !dirty ? 0.5 : 1,
            }}
          >
            {busy ? "…" : "Save"}
          </button>

          {flash && (
            <span style={{ fontFamily: mono, fontSize: 11.5, color: p.green }}>{flash}</span>
          )}
        </div>
      )}

      {err && (
        <div style={{ marginTop: 10, fontSize: 11.5, color: p.red, fontFamily: mono }}>
          {err}
        </div>
      )}
    </section>
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
