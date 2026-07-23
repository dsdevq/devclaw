import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchAllGoals, tokenQueryString, type GoalWithProject } from "../api";
import { phaseColor, phaseIsLive } from "../status";
import { relativeTime } from "../util/time";
import { EmptyState, ErrorNote, Loading, SectionLabel, StatusDot, TieredDisclosure } from "../ui";

// Fleet — the NODE→PROJECT→GOAL drill-down spine (ADR 0008 P1, PR-C). Groups
// every goal under its project, with the same active-shown/settled-folded
// disclosure at BOTH tiers: projects with live goals render in full, quiet
// projects fold; within a project, active goals show and archived ones fold.
// The GOAL tier navigates into the existing GoalDetail (reused, not rebuilt);
// the project header navigates into ProjectDetail. Rows use the console's
// onClick-nav pattern (matching Projects/ProjectDetail), not <Link>.

interface ProjectGroup {
  id: string;
  name: string;
  active: GoalWithProject[];
  settled: GoalWithProject[];
}

function group(goals: GoalWithProject[]): ProjectGroup[] {
  const byId = new Map<string, ProjectGroup>();
  for (const g of goals) {
    let grp = byId.get(g.projectId);
    if (!grp) {
      grp = { id: g.projectId, name: g.projectName, active: [], settled: [] };
      byId.set(g.projectId, grp);
    }
    (g.archived ? grp.settled : grp.active).push(g);
  }
  return [...byId.values()].sort((a, b) => b.active.length - a.active.length);
}

export function Fleet() {
  const nav = useNavigate();
  const qs = tokenQueryString();
  const [goals, setGoals] = useState<GoalWithProject[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => fetchAllGoals().then((r) => alive && setGoals(r)).catch((e) => alive && setErr(String(e)));
    load();
    const t = setInterval(load, 20000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  if (err) return <ErrorNote>{err}</ErrorNote>;
  if (!goals) return <Loading />;

  const openProject = (id: string) => nav(`/projects/${id}${qs}`);
  const openGoal = (id: string) => nav(`/goals/${id}${qs}`);

  const groups = group(goals);
  const liveProjects = groups.filter((p) => p.active.length > 0);
  const quietProjects = groups.filter((p) => p.active.length === 0);

  const block = (p: ProjectGroup) => (
    <ProjectBlock key={p.id} p={p} onOpenProject={openProject} onOpenGoal={openGoal} />
  );

  return (
    <section style={{ marginBottom: 30 }}>
      <SectionLabel count={groups.length}>Fleet</SectionLabel>
      {groups.length === 0 ? (
        <div className="card"><EmptyState title="No projects yet" hint="Register a project and file a goal against it." /></div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {liveProjects.map(block)}
          {quietProjects.length > 0 && (
            <TieredDisclosure label="Quiet projects" count={quietProjects.length}>
              <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>{quietProjects.map(block)}</div>
            </TieredDisclosure>
          )}
        </div>
      )}
    </section>
  );
}

function ProjectBlock({
  p,
  onOpenProject,
  onOpenGoal,
}: {
  p: ProjectGroup;
  onOpenProject: (id: string) => void;
  onOpenGoal: (id: string) => void;
}) {
  return (
    <div className="card" style={{ overflow: "hidden" }}>
      <div
        onClick={() => onOpenProject(p.id)}
        style={{ display: "flex", alignItems: "center", gap: 10, padding: "11px 16px", borderBottom: "1px solid var(--border)", cursor: "pointer" }}
      >
        <span className="mono" style={{ fontWeight: 600, fontSize: 13.5 }}>{p.name}</span>
        <span className="mono muted" style={{ fontSize: 11 }}>{p.active.length} active</span>
      </div>
      {p.active.length > 0 ? (
        p.active.map((g) => <GoalLine key={g.id} g={g} onOpen={onOpenGoal} />)
      ) : (
        <div style={{ padding: "10px 16px" }}><span className="muted" style={{ fontSize: 12.5 }}>No active goals</span></div>
      )}
      {p.settled.length > 0 && (
        <div style={{ padding: "10px 16px", borderTop: "1px solid var(--border)" }}>
          <TieredDisclosure label="Settled goals" count={p.settled.length}>
            <div style={{ marginTop: 6 }}>{p.settled.map((g) => <GoalLine key={g.id} g={g} onOpen={onOpenGoal} />)}</div>
          </TieredDisclosure>
        </div>
      )}
    </div>
  );
}

function GoalLine({ g, onOpen }: { g: GoalWithProject; onOpen: (id: string) => void }) {
  return (
    <div
      onClick={() => onOpen(g.id)}
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(0,1.4fr) 130px minmax(0,1fr) 90px",
        gap: 12,
        alignItems: "center",
        padding: "10px 16px",
        borderBottom: "1px solid var(--border)",
        cursor: "pointer",
      }}
    >
      <span className="mono truncate" style={{ fontWeight: 500, fontSize: 12.5 }}>{g.id}</span>
      <span style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12 }}>
        <StatusDot color={phaseColor(g.phase)} live={phaseIsLive(g.phase)} />
        {g.phaseLabel}
      </span>
      <span className="mono truncate secondary" style={{ fontSize: 11.5 }}>{g.action || "—"}</span>
      <span className="mono secondary" style={{ textAlign: "right", fontSize: 12 }}>{relativeTime(g.lastUpdateMs)}</span>
    </div>
  );
}
