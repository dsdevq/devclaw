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

export type TaskKind =
  | "implement_feature"
  | "fix_bug"
  | "review_repository"
  | "onboard";
export type TaskStatus =
  | "pending"
  | "running"
  | "done"
  | "failed"
  | "cancelled";

export interface TaskRow {
  id: string;
  kind: TaskKind;
  status: TaskStatus;
  goal: string;
  workspaceDir: string;
  parentGoalId: string | null;
  createdAt: number;
  completedAt: number | null;
  prUrl: string | null;
}

export interface ProjectWarning {
  code: string;
  message: string;
  goalIds?: string[];
}

export interface ProjectDetail {
  id: string;
  name: string;
  status: "active" | "paused" | "archived";
  repoUrl: string | null;
  previewUrl: string | null;
  active: GoalRow[];
  archived: GoalRow[];
  /** Recent standalone tasks in this project's workspace (parent_goal_id NULL).
   *  Tasks owned by a goal show up inside that goal, not here — no double-count.
   */
  tasks: TaskRow[];
  /** Advisory warnings — currently only "multiple_active_goals" (warn-first
   *  phase of the one-goal-per-project rule; 2026-07-04). */
  warnings: ProjectWarning[];
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

// A goal plus the project it belongs to — the row shape the Overview and the
// global Goals view render. Aggregated client-side (there's no single all-goals
// endpoint) by fanning out over each project's detail; N is small.
export interface GoalWithProject extends GoalRow {
  projectId: string;
  projectName: string;
  archived: boolean;
}

export async function fetchAllGoals(): Promise<GoalWithProject[]> {
  const projects = await fetchProjects();
  const details = await Promise.all(
    projects.map((pr) => fetchProject(pr.id).catch(() => null)),
  );
  const out: GoalWithProject[] = [];
  details.forEach((d, i) => {
    if (!d) return;
    const proj = projects[i];
    const push = (g: GoalRow, archived: boolean) =>
      out.push({ ...g, projectId: proj.id, projectName: proj.name, archived });
    d.active.forEach((g) => push(g, false));
    d.archived.forEach((g) => push(g, true));
  });
  return out;
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

/** A firming question the goal is blocked on (present only when blocked awaiting
 *  owner answers). Answered via answerGoal. */
export interface Unknown {
  id: string;
  question: string;
  why: string;
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
  blockedKind: string;
  /** Firming questions to answer when blocked awaiting owner input; else []. */
  unknowns: Unknown[];
  projectId?: string;
  /** Every task the goal heartbeat dispatched (parent_goal_id = this goal). */
  tasks: TaskRow[];
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

// ---- dispatch controls (global operator pause + daily run window) --------

export interface OperatorHold {
  on: boolean;
  reason: string;
}

export interface RunSchedule {
  enabled: boolean;
  start: string; // "HH:MM"
  end: string; // "HH:MM"
  tz: string; // IANA name, e.g. "Europe/Kyiv"
}

export interface ControlState {
  operatorHold: OperatorHold;
  schedule: RunSchedule;
  quotaPause: { activeUntilMs: number; reason: string };
  /** True when NEW dispatch is currently gated (by hold, schedule, or quota). */
  blocked: boolean;
  reason: string;
}

export async function fetchControl(): Promise<ControlState> {
  const r = await fetch(`/control.json${tokenQS()}`);
  if (!r.ok) throw new Error(`control.json ${r.status}`);
  return r.json();
}

export async function pauseDispatch(reason?: string): Promise<{ operatorHold: OperatorHold }> {
  const r = await fetch(`/control/pause${tokenQS()}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ reason: reason ?? "" }),
  });
  if (!r.ok) throw new Error(`pause: ${r.status}`);
  return r.json();
}

export async function resumeDispatch(): Promise<{ operatorHold: OperatorHold }> {
  const r = await fetch(`/control/resume${tokenQS()}`, { method: "POST" });
  if (!r.ok) throw new Error(`resume: ${r.status}`);
  return r.json();
}

export async function setSchedule(s: RunSchedule): Promise<{ schedule: RunSchedule }> {
  const r = await fetch(`/control/schedule${tokenQS()}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(s),
  });
  if (!r.ok) {
    let msg = `schedule: ${r.status}`;
    try {
      const j = await r.json();
      if (j?.error) msg = `${j.error}${j.hint ? ` — ${j.hint}` : ""}`;
    } catch {
      /* keep status-code message */
    }
    throw new Error(msg);
  }
  return r.json();
}

// ---- per-goal run window (a night/off-hours narrowing on top of the global
// one; a goal dispatches only if BOTH the global controls and its own window
// allow). Backend: server/http.py GET/POST /goals/{id}/schedule. -------------

export async function fetchGoalSchedule(id: string): Promise<RunSchedule> {
  const r = await fetch(`/goals/${encodeURIComponent(id)}/schedule${tokenQS()}`);
  if (r.status === 404) throw new Error(`goal not found: ${id}`);
  if (!r.ok) throw new Error(`goal schedule ${id}: ${r.status}`);
  const j = await r.json();
  return j.schedule as RunSchedule;
}

// ---- configuration: read-only env catalog (A) + per-project overrides (B) --

export interface EnvVar {
  group: string;
  key: string;
  default: string;
  purpose: string;
  value: string;
  isSet: boolean;
  secret: boolean;
}

export async function fetchEnvConfig(): Promise<EnvVar[]> {
  const r = await fetch(`/config/env.json${tokenQS()}`);
  if (!r.ok) throw new Error(`config/env.json ${r.status}`);
  const j = await r.json();
  return (j.vars ?? []) as EnvVar[];
}

export interface ProjectOverrides {
  automerge: boolean | null;
  autodeploy: boolean | null;
  review_gate: boolean | null;
  verify_done: boolean | null;
  merge_strategy: string | null;
  browser_gate_mode: string | null;
}

export async function fetchProjectConfig(id: string): Promise<ProjectOverrides> {
  const r = await fetch(`/projects/${encodeURIComponent(id)}/config.json${tokenQS()}`);
  if (r.status === 404) throw new Error(`project not found: ${id}`);
  if (!r.ok) throw new Error(`project config ${id}: ${r.status}`);
  const j = await r.json();
  return j.overrides as ProjectOverrides;
}

export async function setProjectConfig(
  id: string,
  patch: Partial<ProjectOverrides>,
): Promise<ProjectOverrides> {
  const r = await fetch(`/projects/${encodeURIComponent(id)}/config${tokenQS()}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!r.ok) {
    let msg = `save config: ${r.status}`;
    try {
      const j = await r.json();
      if (j?.error) msg = `${j.error}${j.field ? ` (${j.field})` : ""}`;
    } catch {
      /* keep status message */
    }
    throw new Error(msg);
  }
  const j = await r.json();
  return j.overrides as ProjectOverrides;
}

export async function setGoalSchedule(
  id: string,
  s: RunSchedule,
): Promise<{ schedule: RunSchedule }> {
  const r = await fetch(`/goals/${encodeURIComponent(id)}/schedule${tokenQS()}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(s),
  });
  if (!r.ok) {
    // 400 bodies carry {error, hint} (bad_time / bad_tz) — surface them the same
    // way the global setSchedule does, so a typo shows the reason not a 400.
    let msg = `goal schedule: ${r.status}`;
    try {
      const j = await r.json();
      if (j?.error) msg = `${j.error}${j.hint ? ` — ${j.hint}` : ""}`;
    } catch {
      /* keep status-code message */
    }
    throw new Error(msg);
  }
  return r.json();
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

export async function resumeGoal(id: string): Promise<{ resumed: boolean; message?: string }> {
  const r = await fetch(`/goals/${encodeURIComponent(id)}/resume${tokenQS()}`, { method: "POST" });
  if (r.status === 404) throw new Error(`goal not found: ${id}`);
  if (!r.ok) {
    let msg = `resume ${id}: ${r.status}`;
    try {
      const j = await r.json();
      if (j?.detail) msg = j.detail;
    } catch {
      /* keep status message */
    }
    throw new Error(msg);
  }
  return r.json();
}

export async function answerGoal(
  id: string,
  answers: Record<string, string>,
): Promise<unknown> {
  const r = await fetch(`/goals/${encodeURIComponent(id)}/answer${tokenQS()}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ answers }),
  });
  if (r.status === 404) throw new Error(`goal not found: ${id}`);
  if (!r.ok) {
    let msg = `answer ${id}: ${r.status}`;
    try {
      const j = await r.json();
      if (j?.detail) msg = j.detail;
    } catch {
      /* keep status message */
    }
    throw new Error(msg);
  }
  return r.json();
}
