import { useEffect, useState } from "react";
import { fetchProjectConfig, setProjectConfig, type ProjectOverrides } from "../api";

// Editable per-project overrides. null = inherit the devclaw-wide default; a
// value pins it for this repo. Live-resolved from the registry (no restart).

type OvrKey = keyof ProjectOverrides;

const BOOL_FIELDS: { key: OvrKey; label: string; hint: string; danger?: boolean }[] = [
  { key: "automerge", label: "Auto-merge", hint: "Merge a PR automatically once its gates pass." },
  { key: "review_gate", label: "Pre-PR review gate", hint: "Adversarial diff review before a PR ships.", danger: true },
  { key: "verify_done", label: "Verify at done-gate", hint: "Re-run the verify command when evaluating completion." },
  { key: "autodeploy", label: "Auto-deploy", hint: "Deploy the app on a successful delivery." },
];

const STR_FIELDS: { key: OvrKey; label: string; hint: string; options: string[] }[] = [
  { key: "merge_strategy", label: "Merge strategy", hint: "How gate-passed PRs are merged.", options: ["squash", "merge", "rebase"] },
  { key: "browser_gate_mode", label: "Browser-gate mode", hint: "Stance when the project has no Playwright suite.", options: ["flexible", "strict"] },
];

function encode(v: boolean | string | null): string {
  if (v === null || v === undefined) return "";
  return typeof v === "boolean" ? String(v) : v;
}

export function ProjectSettings({ projectId }: { projectId: string }) {
  const [draft, setDraft] = useState<ProjectOverrides | null>(null);
  const [saved, setSaved] = useState<ProjectOverrides | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchProjectConfig(projectId)
      .then((o) => {
        if (!alive) return;
        setDraft(o);
        setSaved(o);
      })
      .catch((e) => alive && setErr(String(e instanceof Error ? e.message : e)));
    return () => {
      alive = false;
    };
  }, [projectId]);

  const dirty = !!draft && !!saved && JSON.stringify(draft) !== JSON.stringify(saved);

  const save = async () => {
    if (!draft) return;
    setBusy(true);
    setErr(null);
    try {
      const o = await setProjectConfig(projectId, draft);
      setDraft(o);
      setSaved(o);
      setFlash("Saved");
      setTimeout(() => setFlash(null), 2500);
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  if (!draft) {
    return <div className="secondary" style={{ fontSize: 13, padding: "8px 0" }}>{err ?? "Loading…"}</div>;
  }

  const setField = (k: OvrKey, raw: string, isBool: boolean) =>
    setDraft({ ...draft, [k]: raw === "" ? null : isBool ? raw === "true" : raw });

  return (
    <div className="card" style={{ padding: 4 }}>
      {BOOL_FIELDS.map((f) => (
        <Row key={f.key} label={f.label} hint={f.hint} danger={f.danger && draft[f.key] === false}>
          <select className="field" value={encode(draft[f.key])} onChange={(e) => setField(f.key, e.target.value, true)}>
            <option value="">Inherit</option>
            <option value="true">On</option>
            <option value="false">Off</option>
          </select>
        </Row>
      ))}
      {STR_FIELDS.map((f) => (
        <Row key={f.key} label={f.label} hint={f.hint}>
          <select className="field" value={encode(draft[f.key])} onChange={(e) => setField(f.key, e.target.value, false)}>
            <option value="">Inherit</option>
            {f.options.map((o) => (
              <option key={o} value={o}>{o}</option>
            ))}
          </select>
        </Row>
      ))}
      <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "12px 14px" }}>
        <button className="btn primary sm" disabled={busy || !dirty} onClick={save}>
          {busy ? "Saving…" : "Save overrides"}
        </button>
        {flash && <span className="mono" style={{ fontSize: 12, color: "var(--green)" }}>{flash}</span>}
        {err && <span className="mono" style={{ fontSize: 12, color: "var(--red)" }}>{err}</span>}
      </div>
    </div>
  );
}

function Row({
  label,
  hint,
  danger,
  children,
}: {
  label: string;
  hint: string;
  danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 16, padding: "11px 14px", borderBottom: "1px solid var(--border)" }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 550 }}>{label}</div>
        <div className="secondary" style={{ fontSize: 12, marginTop: 1 }}>
          {hint}
          {danger && <span style={{ color: "var(--amber)" }}> · off means changes ship un-reviewed</span>}
        </div>
      </div>
      <div style={{ flexShrink: 0 }}>{children}</div>
    </div>
  );
}
