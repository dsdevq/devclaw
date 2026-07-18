import { useEffect, useState } from "react";
import {
  fetchControl,
  pauseDispatch,
  resumeDispatch,
  setSchedule,
  type ControlState,
  type RunSchedule,
} from "../api";
import { IconMoon, IconPause, IconPlay, IconSun } from "../icons";
import { useTheme } from "../theme";
import { ErrorNote, Loading, SectionLabel, StatusDot } from "../ui";
import { VERSION } from "../version";

function stateOf(c: ControlState | null): { label: string; color: string; live: boolean } {
  if (!c) return { label: "…", color: "var(--text-muted)", live: false };
  if (c.operatorHold.on) return { label: "Paused", color: "var(--amber)", live: false };
  if (c.blocked && c.schedule.enabled) return { label: "Off-hours", color: "var(--amber)", live: false };
  if (c.blocked) return { label: "Quota held", color: "var(--amber)", live: false };
  return { label: "Running", color: "var(--green)", live: true };
}

export function Settings() {
  const { theme, toggle } = useTheme();
  const [ctrl, setCtrl] = useState<ControlState | null>(null);
  const [draft, setDraft] = useState<RunSchedule | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

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
  }, []);

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

  const s = stateOf(ctrl);

  return (
    <div className="page" style={{ maxWidth: 720 }}>
      <h1 style={{ fontSize: 22, fontWeight: 650, letterSpacing: "-0.02em", margin: "0 0 22px" }}>
        Settings
      </h1>

      {err && <ErrorNote>{err}</ErrorNote>}

      {/* ── Dispatch ─────────────────────────────────────── */}
      <section style={{ marginBottom: 34 }}>
        <SectionLabel
          right={
            <span className="badge">
              <StatusDot color={s.color} live={s.live} />
              {s.label}
            </span>
          }
        >
          Dispatch
        </SectionLabel>

        {!ctrl && !err && <Loading />}
        {ctrl && (
          <div className="card" style={{ padding: 18 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16 }}>
              <div>
                <div style={{ fontSize: 13.5, fontWeight: 550 }}>Manual pause</div>
                <div className="secondary" style={{ fontSize: 12.5, marginTop: 2 }}>
                  Stops all new goal dispatch. In-flight tasks finish.
                </div>
              </div>
              {ctrl.operatorHold.on ? (
                <button className="btn good sm" disabled={busy} onClick={() => run(() => resumeDispatch())}>
                  <IconPlay size={14} /> Resume
                </button>
              ) : (
                <button className="btn danger sm" disabled={busy} onClick={() => run(() => pauseDispatch("paused from console"))}>
                  <IconPause size={14} /> Pause now
                </button>
              )}
            </div>

            <div style={{ height: 1, background: "var(--border)", margin: "18px 0" }} />

            <div style={{ fontSize: 13.5, fontWeight: 550, marginBottom: 4 }}>Daily run window</div>
            <div className="secondary" style={{ fontSize: 12.5, marginBottom: 14 }}>
              Only dispatch inside a time window — the rest of the day is held at zero tokens.
            </div>
            {draft && (
              <>
                <label style={{ display: "flex", alignItems: "center", gap: 9, fontSize: 13, cursor: "pointer", marginBottom: 14 }}>
                  <input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
                  Restrict dispatch to a window
                </label>
                <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                  <input type="time" className="field" value={draft.start} disabled={!draft.enabled} onChange={(e) => setDraft({ ...draft, start: e.target.value })} />
                  <span className="muted" style={{ fontSize: 12 }}>to</span>
                  <input type="time" className="field" value={draft.end} disabled={!draft.enabled} onChange={(e) => setDraft({ ...draft, end: e.target.value })} />
                  <input type="text" className="field" style={{ width: 160 }} value={draft.tz} spellCheck={false} disabled={!draft.enabled} placeholder="Europe/Kyiv" onChange={(e) => setDraft({ ...draft, tz: e.target.value })} />
                  <button className="btn primary sm" disabled={busy} onClick={() => draft && run(() => setSchedule(draft))}>Save</button>
                </div>
              </>
            )}
            {ctrl.blocked && ctrl.reason && (
              <div className="mono" style={{ fontSize: 12, color: "var(--amber)", marginTop: 14 }}>Held: {ctrl.reason}</div>
            )}
          </div>
        )}
      </section>

      {/* ── Appearance ───────────────────────────────────── */}
      <section style={{ marginBottom: 34 }}>
        <SectionLabel>Appearance</SectionLabel>
        <div className="card" style={{ padding: 18, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16 }}>
          <div>
            <div style={{ fontSize: 13.5, fontWeight: 550 }}>Theme</div>
            <div className="secondary" style={{ fontSize: 12.5, marginTop: 2 }}>Dark is built for long monitoring sessions.</div>
          </div>
          <button className="btn sm" onClick={toggle} style={{ minWidth: 110 }}>
            {theme === "dark" ? <IconMoon size={14} /> : <IconSun size={14} />}
            {theme === "dark" ? "Dark" : "Light"}
          </button>
        </div>
      </section>

      {/* ── About ────────────────────────────────────────── */}
      <section>
        <SectionLabel>About</SectionLabel>
        <div className="card" style={{ padding: 18 }}>
          <Row label="Console" value={`v${VERSION}`} />
          <Row label="Surface" value="Read-write operator console over the devclaw MCP" />
        </div>
      </section>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 16, padding: "6px 0", fontSize: 13 }}>
      <span className="secondary">{label}</span>
      <span className="mono" style={{ textAlign: "right" }}>{value}</span>
    </div>
  );
}
