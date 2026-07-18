import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { fetchProject, tokenQueryString, type GoalRow, type ProjectDetail as PD } from "../api";
import { IconAlert, IconExternal } from "../icons";
import { phaseColor, phaseIsLive } from "../status";
import { relativeTime } from "../util/time";
import { EmptyState, ErrorNote, Loading, SectionLabel, StatusDot } from "../ui";
import { ProjectSettings } from "../components/ProjectSettings";
import { TasksSection } from "../components/TasksSection";

const COLS = "minmax(0,1.3fr) 140px minmax(0,1fr) 100px";

export function ProjectDetail() {
  const { id } = useParams<{ id: string }>();
  const nav = useNavigate();
  const qs = tokenQueryString();
  const [data, setData] = useState<PD | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [showArchived, setShowArchived] = useState(false);

  useEffect(() => {
    if (!id) return;
    let alive = true;
    setData(null);
    setErr(null);
    fetchProject(id).then((d) => alive && setData(d)).catch((e) => alive && setErr(String(e)));
    return () => {
      alive = false;
    };
  }, [id]);

  const goalRow = (g: GoalRow) => (
    <div
      key={g.id}
      className="rowlink"
      onClick={() => nav(`/goals/${g.id}${qs}`)}
      style={{
        display: "grid",
        gridTemplateColumns: COLS,
        gap: 16,
        alignItems: "center",
        padding: "12px 16px",
        borderBottom: "1px solid var(--border)",
      }}
    >
      <span className="mono truncate" style={{ fontWeight: 500, fontSize: 13 }}>{g.id}</span>
      <span style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12.5 }}>
        <StatusDot color={phaseColor(g.phase)} live={phaseIsLive(g.phase)} />
        {g.phaseLabel}
      </span>
      <span className="mono truncate secondary" style={{ fontSize: 12 }}>{g.action || "—"}</span>
      <span className="mono secondary" style={{ textAlign: "right", fontSize: 12.5 }}>{relativeTime(g.lastUpdateMs)}</span>
    </div>
  );

  return (
    <div className="page">
      <Link to={`/projects${qs}`} className="secondary" style={{ fontSize: 12.5 }}>← Projects</Link>

      {err && <ErrorNote>{err}</ErrorNote>}
      {!data && !err && <Loading />}

      {data && (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 12, margin: "14px 0 10px" }}>
            <h1 className="mono" style={{ fontSize: 24, fontWeight: 600, letterSpacing: "-0.01em", margin: 0 }}>
              {data.name}
            </h1>
            <StatusDot
              color={data.status === "active" ? "var(--green)" : data.status === "paused" ? "var(--amber)" : "var(--text-muted)"}
              live={data.status === "active"}
            />
            <span className="secondary" style={{ fontSize: 13 }}>
              {data.status[0].toUpperCase() + data.status.slice(1)}
            </span>
          </div>

          <div style={{ display: "flex", gap: 20, flexWrap: "wrap", marginBottom: 26 }}>
            {data.repoUrl && <MetaLink label="GitHub" url={data.repoUrl} />}
            {data.previewUrl && <MetaLink label="Live preview" url={data.previewUrl} />}
            {!data.repoUrl && !data.previewUrl && (
              <span className="muted" style={{ fontSize: 12.5 }}>No repo or preview registered</span>
            )}
          </div>

          {(data.warnings ?? []).map((w) => (
            <div
              key={w.code}
              className="card"
              style={{ display: "flex", gap: 11, padding: "12px 14px", marginBottom: 20, borderColor: "color-mix(in srgb, var(--amber) 45%, var(--border))", background: "var(--amber-soft)" }}
            >
              <span style={{ color: "var(--amber)", flexShrink: 0 }}><IconAlert size={17} /></span>
              <div style={{ fontSize: 12.5, lineHeight: 1.5 }}>
                <div style={{ fontWeight: 600, marginBottom: 2 }}>
                  {w.code === "multiple_active_goals" ? "Multiple active goals" : w.code}
                </div>
                <div className="secondary">{w.message}</div>
              </div>
            </div>
          ))}

          <section style={{ marginBottom: 30 }}>
            <SectionLabel count={data.active.length}>Active goals</SectionLabel>
            {data.active.length > 0 ? (
              <div className="card" style={{ overflow: "hidden" }}>{data.active.map(goalRow)}</div>
            ) : (
              <div className="card"><EmptyState title="No active goals" hint="File one from a devclaw session or Telegram." /></div>
            )}
          </section>

          {data.archived.length > 0 && (
            <section style={{ marginBottom: 30 }}>
              <SectionLabel
                count={data.archived.length}
                right={
                  <button className="btn ghost sm" onClick={() => setShowArchived((s) => !s)}>
                    {showArchived ? "Hide" : "Show"}
                  </button>
                }
              >
                Archived goals
              </SectionLabel>
              {showArchived && (
                <div className="card" style={{ overflow: "hidden", opacity: 0.75 }}>{data.archived.map(goalRow)}</div>
              )}
            </section>
          )}

          <section style={{ marginBottom: 30 }}>
            <SectionLabel count={(data.tasks ?? []).length}>Recent tasks</SectionLabel>
            <TasksSection
              tasks={data.tasks ?? []}
              emptyLabel="No standalone tasks — features/fixes dispatched without a goal land here."
            />
          </section>

          <section>
            <SectionLabel>Settings · overrides</SectionLabel>
            {id && <ProjectSettings projectId={id} />}
          </section>
        </>
      )}
    </div>
  );
}

function MetaLink({ label, url }: { label: string; url: string }) {
  return (
    <a href={url} target="_blank" rel="noreferrer" style={{ display: "flex", alignItems: "baseline", gap: 7 }}>
      <span className="eyebrow">{label}</span>
      <span className="mono" style={{ fontSize: 12.5, display: "inline-flex", alignItems: "center", gap: 5 }}>
        {url.replace(/^https?:\/\//, "")}
        <IconExternal size={12} />
      </span>
    </a>
  );
}
