import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchProblems, type ProblemRow, type ProblemStage, type ProblemsResponse } from "../api";
import { relativeTime } from "../util/time";
import { IconExternal } from "../icons";
import { EmptyState, ErrorNote, Loading, SectionLabel, StatusDot } from "../ui";

// Problems — the problem-lifecycle tracker (ADR 0009 P2 + N2/#372). Renders the
// deduplicated problems catalog as the self-improving loop: each entry moves
// identified → filed → fixing → resolved — the full "issue → reviewed PR" story.
// HONEST (§5.5): `fixing` means a self-fix goal is running and a PR opens for
// YOUR review — it is propose-only/human-merges, never autonomous auto-fix, so
// the UI never implies autonomy that isn't there.

const STAGE_COLOR: Record<ProblemStage, string> = {
  identified: "var(--amber)",
  filed: "var(--accent)",
  fixing: "var(--violet)",
  resolved: "var(--green)",
};
const STAGE_LABEL: Record<ProblemStage, string> = {
  identified: "Identified",
  filed: "Filed",
  fixing: "Fixing",
  resolved: "Resolved",
};
const STAGES: (ProblemStage | "all")[] = ["all", "identified", "filed", "fixing", "resolved"];

export function Problems() {
  const [data, setData] = useState<ProblemsResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [stage, setStage] = useState<ProblemStage | "all">("all");

  useEffect(() => {
    let alive = true;
    const load = () => fetchProblems().then((r) => alive && setData(r)).catch((e) => alive && setErr(String(e)));
    load();
    const t = setInterval(load, 20000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const problems = data?.problems ?? [];
  const countBy = (s: ProblemStage) => problems.filter((p) => p.lifecycle === s).length;
  const shown = stage === "all" ? problems : problems.filter((p) => p.lifecycle === stage);

  return (
    <div className="page">
      <h1 style={{ fontSize: 22, fontWeight: 650, letterSpacing: "-0.02em", margin: "0 0 18px" }}>
        Problems
      </h1>

      {err && <ErrorNote>{err}</ErrorNote>}
      {!data && !err && <Loading />}

      {data && (
        <>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 22 }}>
            <Tile label="Problems" value={problems.length} />
            <Tile label="Identified" value={countBy("identified")} color="var(--amber)" />
            <Tile label="Filed" value={countBy("filed")} color="var(--accent)" />
            <Tile label="Fixing" value={countBy("fixing")} color="var(--violet)" />
            <Tile label="Resolved" value={countBy("resolved")} color="var(--green)" />
          </div>

          {data.selfRepo === null && (
            <div className="card" style={{ padding: "10px 14px", marginBottom: 18 }}>
              <span className="secondary" style={{ fontSize: 12.5 }}>
                Self-issue-filing is off (no <span className="mono">DEVCLAW_SELF_REPO</span>) — problems are still
                catalogued and shown, but none get filed as issues.
              </span>
            </div>
          )}

          <SectionLabel
            count={shown.length}
            right={
              <div style={{ display: "flex", gap: 6 }}>
                {STAGES.map((s) => (
                  <button
                    key={s}
                    className={`btn ghost sm${stage === s ? " active" : ""}`}
                    onClick={() => setStage(s)}
                    style={stage === s ? { color: "var(--accent)" } : undefined}
                  >
                    {s}
                  </button>
                ))}
              </div>
            }
          >
            Catalog
          </SectionLabel>

          {shown.length === 0 ? (
            <div className="card"><EmptyState title="Nothing here" hint="No problems at this lifecycle stage." /></div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {shown.map((p) => <ProblemCard key={p.fingerprint} p={p} selfRepo={data.selfRepo} />)}
            </div>
          )}

          <div className="muted" style={{ fontSize: 11, marginTop: 14 }}>
            Lifecycle: <b>identified</b> (in the catalog) → <b>filed</b> (a GitHub issue is open) → <b>fixing</b>
            (a self-fix goal is running) → <b>resolved</b> (issue closed). <b>Fixing</b> opens a PR for <b>your</b>{" "}
            review — it is <b>propose-only, human-merges</b>, never autonomous auto-fix.
          </div>
        </>
      )}
    </div>
  );
}

function ProblemCard({ p, selfRepo }: { p: ProblemRow; selfRepo: string | null }) {
  const nav = useNavigate();
  const issueUrl = selfRepo && p.issue_number ? `https://github.com/${selfRepo}/issues/${p.issue_number}` : null;
  return (
    <div className="card" style={{ padding: "12px 16px", borderLeft: `2px solid ${STAGE_COLOR[p.lifecycle]}` }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
        <StatusDot color={STAGE_COLOR[p.lifecycle]} />
        <span style={{ fontSize: 12.5, fontWeight: 600 }}>{STAGE_LABEL[p.lifecycle]}</span>
        <span className="mono muted" style={{ fontSize: 11 }}>{p.category} · {p.kind}</span>
        <span className="mono muted" style={{ marginLeft: "auto", fontSize: 11 }}>×{p.count}</span>
      </div>
      <div className="secondary" style={{ fontSize: 12.5, whiteSpace: "pre-wrap", wordBreak: "break-word", marginBottom: 8 }}>
        {p.summary}
      </div>
      {p.lifecycle === "fixing" && p.fix_goal_id && (
        <button
          className="btn ghost sm"
          onClick={() => nav(`/goals/${p.fix_goal_id}`)}
          style={{ color: "var(--violet)", marginBottom: 8, display: "inline-flex", alignItems: "center", gap: 5 }}
        >
          ▸ fix goal running — open PR for your review
        </button>
      )}
      <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
        <Stat label="terminal" value={p.terminal_count} tone={p.terminal_count ? "var(--red)" : undefined} />
        <Stat label="recovered" value={p.recovered_count} tone={p.recovered_count ? "var(--green)" : undefined} />
        <span className="mono muted" style={{ fontSize: 11 }}>last {relativeTime(p.last_seen_ms)}</span>
        {issueUrl ? (
          <a href={issueUrl} target="_blank" rel="noreferrer" style={{ marginLeft: "auto", fontSize: 12, display: "inline-flex", alignItems: "center", gap: 4 }}>
            issue #{p.issue_number} <IconExternal size={12} />
          </a>
        ) : p.issue_number ? (
          <span className="mono muted" style={{ marginLeft: "auto", fontSize: 11 }}>issue #{p.issue_number}</span>
        ) : null}
      </div>
    </div>
  );
}

function Tile({ label, value, color }: { label: string; value: number; color?: string }) {
  return (
    <div className="card" style={{ padding: "14px 18px", minWidth: 120, flex: "1 1 120px" }}>
      <div style={{ fontSize: 26, fontWeight: 650, letterSpacing: "-0.02em", color: color ?? "var(--text)" }}>{value}</div>
      <div className="eyebrow" style={{ marginTop: 4 }}>{label}</div>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <span style={{ fontSize: 11.5 }}>
      <span className={tone ? "mono" : "mono secondary"} style={tone ? { color: tone } : undefined}>{value}</span>{" "}
      <span className="muted">{label}</span>
    </span>
  );
}
