// Thin fetch wrappers. The bearer-token query string is preserved from the
// current URL so the console works when devclaw is served behind the
// existing DEVCLAW_TOKEN gate (see server/lifecycle.py bearer auth).

function tokenQS(): string {
  if (typeof window === "undefined") return "";
  const tok = new URLSearchParams(window.location.search).get("token");
  return tok ? `?token=${encodeURIComponent(tok)}` : "";
}

export interface ProjectRow {
  id: string;
  name: string;
  status: "active" | "paused" | "archived";
  activeGoals: number;
  lastActivityMs: number | null;
}

export async function fetchProjects(): Promise<ProjectRow[]> {
  const r = await fetch(`/projects.json${tokenQS()}`);
  if (!r.ok) throw new Error(`projects.json ${r.status}`);
  return r.json();
}

export interface GoalRow {
  id: string;
  phase: string | null;
  phaseLabel: string;
  action: string;
  lastUpdateMs: number | null;
}

export interface ProjectDetail {
  id: string;
  name: string;
  status: "active" | "paused" | "archived";
  repoUrl: string | null;
  previewUrl: string | null;
  active: GoalRow[];
  archived: GoalRow[];
}

export async function fetchProject(id: string): Promise<ProjectDetail> {
  const r = await fetch(`/projects/${encodeURIComponent(id)}.json${tokenQS()}`);
  if (r.status === 404) throw new Error(`project not found: ${id}`);
  if (!r.ok) throw new Error(`project ${id}: ${r.status}`);
  return r.json();
}

export function tokenQueryString(): string {
  return tokenQS();
}

export type Verdict =
  | "on_track"
  | "off_track"
  | "achieved"
  | "stalled"
  | "needs_human";

export interface TimelineNode {
  name: string;
  reached: boolean;
  current: boolean;
  timestampMs: number | null;
}

export interface GoalDetail {
  id: string;
  objective: string;
  phase: string | null;
  phaseLabel: string;
  lifecycle: string | null;
  direction: { verdict: Verdict; at: string; note: string } | null;
  actionsDispatched: number;
  inFlight: { tool: string; id: string; is_done_check: boolean } | null;
  timeline: TimelineNode[];
  blockedOn: string | null;
  projectId?: string;
}

export async function fetchGoal(id: string): Promise<GoalDetail> {
  const r = await fetch(`/goals/${encodeURIComponent(id)}.json${tokenQS()}`);
  if (r.status === 404) throw new Error(`goal not found: ${id}`);
  if (!r.ok) throw new Error(`goal ${id}: ${r.status}`);
  return r.json();
}
