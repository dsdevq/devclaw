import { useEffect, useMemo, useState } from "react";
import { fetchEnvConfig, type EnvVar } from "../api";
import { ErrorNote, Loading } from "../ui";

// Read-only catalog of every runtime env var, parsed live from the reference
// doc on the server. Editing global env needs a container restart, so this is
// view-only — the editable knobs are per-project (ProjectSettings).

export function EnvConfigTable() {
  const [rows, setRows] = useState<EnvVar[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");

  useEffect(() => {
    let alive = true;
    fetchEnvConfig()
      .then((r) => alive && setRows(r))
      .catch((e) => alive && setErr(String(e)));
    return () => {
      alive = false;
    };
  }, []);

  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const filtered = (rows ?? []).filter(
      (v) => !needle || v.key.toLowerCase().includes(needle) || v.purpose.toLowerCase().includes(needle),
    );
    const by = new Map<string, EnvVar[]>();
    for (const v of filtered) {
      const list = by.get(v.group) ?? [];
      list.push(v);
      by.set(v.group, list);
    }
    return [...by.entries()];
  }, [rows, q]);

  if (err) return <ErrorNote>{err}</ErrorNote>;
  if (!rows) return <Loading />;

  return (
    <>
      <input
        className="field"
        style={{ width: "100%", marginBottom: 14, fontFamily: "var(--sans)" }}
        placeholder="Filter variables…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      <p className="secondary" style={{ fontSize: 12.5, margin: "0 0 16px" }}>
        Read-only — global env is read at container start. To change a value, edit the deploy
        env and redeploy. Per-repo behaviour is editable under each project's Settings.
      </p>
      {groups.map(([group, vars]) => (
        <div key={group} style={{ marginBottom: 24 }}>
          <div className="eyebrow" style={{ marginBottom: 8 }}>{group}</div>
          <div className="card" style={{ overflow: "hidden" }}>
            {vars.map((v) => (
              <div key={v.key} style={{ padding: "11px 14px", borderBottom: "1px solid var(--border)" }}>
                <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
                  <span className="mono" style={{ fontSize: 12.5, fontWeight: 500 }}>{v.key}</span>
                  {v.isSet ? (
                    <span className="mono" style={{ fontSize: 12, color: "var(--accent)" }}>
                      = {v.value || "(set)"}
                    </span>
                  ) : (
                    <span className="mono muted" style={{ fontSize: 12 }}>
                      default{v.default ? ` = ${v.default}` : ""}
                    </span>
                  )}
                  {v.secret && <span className="badge" style={{ height: 18, fontSize: 10 }}>secret</span>}
                </div>
                <div className="secondary" style={{ fontSize: 12, marginTop: 3, lineHeight: 1.45 }}>{v.purpose}</div>
              </div>
            ))}
          </div>
        </div>
      ))}
      {groups.length === 0 && <div className="muted" style={{ fontSize: 13 }}>No variables match.</div>}
    </>
  );
}
