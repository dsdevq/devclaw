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
  dispatchCap: number;
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

export type EventKind = "cognition" | "subprocess" | "dispatch" | "delivery" | "notify";
export const EVENT_KINDS: EventKind[] = [
  "cognition",
  "subprocess",
  "dispatch",
  "delivery",
  "notify",
];

export interface StreamEvent {
  id: number;
  kind: EventKind;
  type: string;
  source: string;
  ts: number | string;
  payload: unknown;
}

export function goalEventsUrl(id: string): string {
  return `/goals/${encodeURIComponent(id)}/events${tokenQS()}`;
}

export async function cancelGoal(id: string): Promise<{ cancelled: boolean; phase: string; reason?: string }> {
  const r = await fetch(`/goals/${encodeURIComponent(id)}/cancel${tokenQS()}`, {
    method: "POST",
  });
  if (r.status === 404) throw new Error(`goal not found: ${id}`);
  if (!r.ok) throw new Error(`cancel ${id}: ${r.status}`);
  return r.json();
}

export type PRState = "OPEN" | "MERGED" | "CLOSED" | "UNKNOWN";
export type PRMergeable = "MERGEABLE" | "CONFLICTING" | "UNKNOWN";

export interface PRRow {
  prUrl: string;
  prNumber: number;
  repo: string;
  actionLabel: string;
  gatePassed: boolean | null;
  ts: string;
  state: PRState;
  mergeable: PRMergeable;
  mergeStateStatus: string | null;
  title: string;
  mergedAt: string | null;
  error?: string;
}

export async function fetchGoalPrs(id: string): Promise<PRRow[]> {
  const r = await fetch(`/goals/${encodeURIComponent(id)}/prs.json${tokenQS()}`);
  if (r.status === 404) throw new Error(`goal not found: ${id}`);
  if (!r.ok) throw new Error(`goal prs ${id}: ${r.status}`);
  const j = await r.json();
  return (j.prs ?? []) as PRRow[];
}

export async function mergePr(prUrl: string): Promise<{ merged: boolean; error?: string }> {
  const r = await fetch(`/prs/merge${tokenQS()}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ prUrl }),
  });
  if (r.ok) return r.json();
  // 4xx/5xx bodies also carry {merged:false, error} — surface that shape.
  try {
    return await r.json();
  } catch {
    return { merged: false, error: `merge failed: ${r.status}` };
  }
}

export async function steerGoal(id: string, message: string): Promise<{ steered: boolean }> {
  const r = await fetch(`/goals/${encodeURIComponent(id)}/steer${tokenQS()}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (r.status === 404) throw new Error(`goal not found: ${id}`);
  if (!r.ok) {
    const err = await r.text();
    throw new Error(`steer ${id}: ${r.status} ${err}`);
  }
  return r.json();
}
