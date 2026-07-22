import { useEffect, useState, type ReactNode } from "react";
import {
  fetchEvalOutcomes,
  fetchNightReports,
  type EvalOutcome,
  type NightReport,
} from "../api";
import { KIND_LABEL, taskStatusColor } from "../status";
import { relativeTime } from "../util/time";
import { EmptyState, ErrorNote, Loading, SectionLabel, StatusDot } from "../ui";

// The Evals tab is a read-only projection of the eval_outcomes table (every
// settled task + ingested basket runs) plus the night_reports table (the
// nightly window-close report). Two headline metrics per ADR 0006:
//   * pass_rate — fraction of settled outcomes that are done AND verify-passed;
//   * clean-night rate — fraction of nights with zero mechanism-wedges.
// Both feature-detect empty/missing tables and render an empty state, not a crash.

type SourceFilter = "all" | "live" | "basket";

export function Evals() {
  const [outcomes, setOutcomes] = useState<EvalOutcome[] | null>(null);
  const [nights, setNights] = useState<NightReport[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<SourceFilter>("all");

  useEffect(() => {
    let alive = true;
    const load = () =>
      Promise.all([fetchEvalOutcomes({ limit: 200 }), fetchNightReports(60)])
        .then(([o, n]) => {
          if (!alive) return;
          setOutcomes(o);
          setNights(n);
        })
        .catch((e) => alive && setErr(String(e)));
    load();
    const t = setInterval(load, 20000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const rows = outcomes ?? [];
  const settled = rows.length;
  const passed = rows.filter((o) => o.status === "done" && o.verify_passed === 1).length;
  const passRate = settled ? passed / settled : null;

  const nightRows = nights ?? [];
  const cleanNights = nightRows.filter((n) => n.clean === 1).length;
  const cleanRate = nightRows.length ? cleanNights / nightRows.length : null;

  const shown =
    filter === "all" ? rows : rows.filter((o) => o.source === filter);

  return (
    <div className="page">
      <h1 style={{ fontSize: 22, fontWeight: 650, letterSpacing: "-0.02em", margin: "0 0 18px" }}>
        Evals
      </h1>

      {err && <ErrorNote>{err}</ErrorNote>}
      {!outcomes && !err && <Loading />}

      {outcomes && (
        <>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 30 }}>
            <Metric
              label="Pass rate"
              value={pct(passRate)}
              sub={settled ? `${passed} / ${settled} settled` : "no outcomes yet"}
              color={rateColor(passRate)}
            />
            <Metric
              label="Clean nights"
              value={pct(cleanRate)}
              sub={
                nightRows.length
                  ? `${cleanNights} / ${nightRows.length} nights`
                  : "no night reports yet"
              }
              color={rateColor(cleanRate)}
            />
            <Metric label="Outcomes" value={String(settled)} sub="recent settles" />
          </div>

          <section style={{ marginBottom: 34 }}>
            <SectionLabel
              count={shown.length}
              right={
                <div style={{ display: "flex", gap: 6 }}>
                  {(["all", "live", "basket"] as SourceFilter[]).map((f) => (
                    <button
                      key={f}
                      className={`btn ghost sm${filter === f ? " active" : ""}`}
                      onClick={() => setFilter(f)}
                      style={filter === f ? { color: "var(--accent)" } : undefined}
                    >
                      {f}
                    </button>
                  ))}
                </div>
              }
            >
              Outcomes
            </SectionLabel>
            {shown.length === 0 ? (
              <div className="card">
                <EmptyState
                  title="No outcomes"
                  hint="Settled tasks and ingested basket runs appear here."
                />
              </div>
            ) : (
              <OutcomesTable rows={shown} />
            )}
          </section>

          <section>
            <SectionLabel count={nightRows.length}>Night reports</SectionLabel>
            {nightRows.length === 0 ? (
              <div className="card">
                <EmptyState
                  title="No night reports yet"
                  hint="The nightly window-close report lands here once the night-report tranche ships."
                />
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {nightRows.map((n) => (
                  <NightRow key={n.night_date} n={n} />
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

function pct(r: number | null): string {
  return r === null ? "—" : `${Math.round(r * 100)}%`;
}

function rateColor(r: number | null): string {
  if (r === null) return "var(--text-muted)";
  if (r >= 0.8) return "var(--green)";
  if (r >= 0.5) return "var(--amber)";
  return "var(--red)";
}

function Metric({
  label,
  value,
  sub,
  color,
}: {
  label: string;
  value: string;
  sub: string;
  color?: string;
}) {
  return (
    <div className="card" style={{ padding: "14px 18px", minWidth: 150, flex: "1 1 150px" }}>
      <div
        style={{
          fontSize: 26,
          fontWeight: 650,
          letterSpacing: "-0.02em",
          color: color ?? "var(--text)",
        }}
      >
        {value}
      </div>
      <div className="eyebrow" style={{ marginTop: 4 }}>
        {label}
      </div>
      <div className="secondary" style={{ fontSize: 12, marginTop: 2 }}>
        {sub}
      </div>
    </div>
  );
}

function verifyGlyph(v: number | null): { text: string; color: string } {
  if (v === 1) return { text: "pass", color: "var(--green)" };
  if (v === 0) return { text: "fail", color: "var(--red)" };
  return { text: "—", color: "var(--text-muted)" };
}

function OutcomesTable({ rows }: { rows: EvalOutcome[] }) {
  return (
    <div className="card" style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
        <thead>
          <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
            <Th>Source</Th>
            <Th>Ticket / task</Th>
            <Th>Kind</Th>
            <Th>Status</Th>
            <Th>Verify</Th>
            <Th>PR</Th>
            <Th>Failure</Th>
            <Th>Settled</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((o) => {
            const v = verifyGlyph(o.verify_passed);
            const ref = o.ticket ?? o.task_id ?? "—";
            return (
              <tr key={o.id} style={{ borderTop: "1px solid var(--border)" }}>
                <Td>
                  <span className="badge">{o.source}</span>
                </Td>
                <Td>
                  <span className="mono truncate" style={{ maxWidth: 180, display: "inline-block" }}>
                    {ref}
                  </span>
                </Td>
                <Td>{o.kind ? KIND_LABEL[o.kind] ?? o.kind : "—"}</Td>
                <Td>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <StatusDot color={taskStatusColor(o.status)} />
                    {o.status}
                  </span>
                </Td>
                <Td>
                  <span style={{ color: v.color }}>{v.text}</span>
                </Td>
                <Td>
                  {o.pr_url ? (
                    <a href={o.pr_url} target="_blank" rel="noreferrer" className="mono">
                      PR
                    </a>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </Td>
                <Td>
                  {o.failure_class ? (
                    <span className="mono" style={{ color: "var(--red)" }}>
                      {o.failure_class}
                    </span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </Td>
                <Td>
                  <span className="mono muted">{relativeTime(o.settled_at)}</span>
                </Td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Th({ children }: { children: ReactNode }) {
  return (
    <th className="eyebrow" style={{ padding: "10px 12px", fontWeight: 500, whiteSpace: "nowrap" }}>
      {children}
    </th>
  );
}

function Td({ children }: { children: ReactNode }) {
  return <td style={{ padding: "10px 12px", whiteSpace: "nowrap" }}>{children}</td>;
}

function NightRow({ n }: { n: NightReport }) {
  const wedges = safeLen(n.wedges_json);
  const pauses = safeLen(n.pauses_json);
  const color = n.clean === 1 ? "var(--green)" : "var(--red)";
  return (
    <div className="card" style={{ padding: "12px 16px", borderLeft: `2px solid ${color}` }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
        <StatusDot color={color} />
        <span className="mono" style={{ fontWeight: 550, fontSize: 13.5 }}>
          {n.night_date}
        </span>
        <span className="badge" style={{ marginLeft: "auto" }}>
          {n.clean === 1 ? "clean" : `${wedges} wedge${wedges === 1 ? "" : "s"}`}
        </span>
        {pauses > 0 && (
          <span className="badge" title="self-healed pauses (not wedges)">
            {pauses} pause{pauses === 1 ? "" : "s"}
          </span>
        )}
      </div>
      <div className="secondary" style={{ fontSize: 12.5, paddingLeft: 17, whiteSpace: "pre-wrap" }}>
        {n.summary || "—"}
      </div>
    </div>
  );
}

function safeLen(jsonStr: string): number {
  try {
    const v = JSON.parse(jsonStr);
    return Array.isArray(v) ? v.length : 0;
  } catch {
    return 0;
  }
}
