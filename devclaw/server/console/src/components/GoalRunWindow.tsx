import { useEffect, useState } from "react";
import { fetchGoalSchedule, setGoalSchedule, type RunSchedule } from "../api";

// Per-goal run window: a nights/off-hours narrowing on top of the GLOBAL window.
// A goal dispatches only if BOTH the global controls AND its own window allow.

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
      })
      .catch((e) => live && setErr(String(e instanceof Error ? e.message : e)));
    return () => {
      live = false;
    };
  }, [goalId]);

  const dirty =
    !!draft && !!saved &&
    (draft.enabled !== saved.enabled || draft.start !== saved.start ||
      draft.end !== saved.end || draft.tz !== saved.tz);

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

  if (!draft) return <div className="secondary" style={{ fontSize: 13 }}>{err ?? "Loading…"}</div>;

  return (
    <div className="card" style={{ padding: 18, maxWidth: 560 }}>
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>Run window</div>
      <p className="secondary" style={{ fontSize: 12.5, margin: "0 0 16px" }}>
        Confine this goal to a time window on top of the global schedule — useful for
        pinning a token-heavy standing goal to nights.
      </p>

      <label style={{ display: "flex", alignItems: "center", gap: 9, fontSize: 13, cursor: "pointer", marginBottom: 14 }}>
        <input
          type="checkbox"
          checked={draft.enabled}
          onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}
        />
        Only dispatch inside a time window
      </label>

      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <input
          type="time"
          className="field"
          value={draft.start}
          disabled={!draft.enabled}
          onChange={(e) => setDraft({ ...draft, start: e.target.value })}
        />
        <span className="muted" style={{ fontSize: 12 }}>to</span>
        <input
          type="time"
          className="field"
          value={draft.end}
          disabled={!draft.enabled}
          onChange={(e) => setDraft({ ...draft, end: e.target.value })}
        />
        <input
          type="text"
          className="field"
          style={{ width: 160 }}
          value={draft.tz}
          spellCheck={false}
          disabled={!draft.enabled}
          placeholder="Europe/Kyiv"
          onChange={(e) => setDraft({ ...draft, tz: e.target.value })}
        />
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 16 }}>
        <button className="btn primary sm" disabled={busy || !dirty} onClick={save}>
          {busy ? "Saving…" : "Save window"}
        </button>
        {flash && <span className="mono" style={{ fontSize: 12, color: "var(--green)" }}>{flash}</span>}
        {err && <span className="mono" style={{ fontSize: 12, color: "var(--red)" }}>{err}</span>}
      </div>
    </div>
  );
}
