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
