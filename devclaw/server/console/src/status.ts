// Semantic → color/label mappings, in ONE place, as CSS-variable strings so they
// track the active theme. Every screen reads meaning from here instead of
// re-deriving colors inline.
import type { PRRow, TaskStatus, Verdict } from "./api";

export const C = {
  accent: "var(--accent)",
  green: "var(--green)",
  amber: "var(--amber)",
  red: "var(--red)",
  secondary: "var(--text-secondary)",
  muted: "var(--text-muted)",
};

// A goal's lifecycle phase → dot color + whether it's "live" (pulsing).
export function phaseColor(phase: string | null): string {
  switch (phase) {
    case "in_flight":
    case "verifying":
      return C.accent;
    case "executing":
    case "firming":
    case "investigating":
      return C.accent;
    case "done":
    case "achieved":
      return C.green;
    case "blocked":
      return C.amber;
    case "cancelled":
    case "error":
      return C.red;
    case "idle":
      return C.secondary;
    default:
      return C.muted;
  }
}

export function phaseIsLive(phase: string | null): boolean {
  return phase === "in_flight" || phase === "verifying" || phase === "executing";
}

export function verdictColor(v: Verdict | null | undefined): string {
  switch (v) {
    case "achieved":
    case "on_track":
      return C.green;
    case "off_track":
    case "stalled":
      return C.amber;
    case "needs_human":
      return C.red;
    default:
      return C.muted;
  }
}

export const VERDICT_LABEL: Record<Verdict, string> = {
  on_track: "On track",
  off_track: "Off track",
  achieved: "Achieved",
  stalled: "Stalled",
  needs_human: "Needs human",
};

export function taskStatusColor(s: TaskStatus | string): string {
  switch (s) {
    case "done":
      return C.green;
    case "running":
      return C.accent;
    case "failed":
      return C.red;
    case "pending":
      return C.secondary;
    default:
      return C.muted;
  }
}

export const KIND_LABEL: Record<string, string> = {
  implement_feature: "feature",
  fix_bug: "bug fix",
  review_repository: "review",
  onboard: "onboard",
};

// PR state → { color, label } for the merge rows.
export function prMeta(row: PRRow): { color: string; label: string; canMerge: boolean } {
  const canMerge = row.state === "OPEN" && row.mergeable === "MERGEABLE";
  const color =
    row.state === "MERGED"
      ? C.green
      : row.state === "CLOSED"
        ? C.muted
        : row.mergeable === "CONFLICTING"
          ? C.red
          : row.mergeable === "MERGEABLE"
            ? C.green
            : C.amber;
  const label = row.state === "OPEN" ? row.mergeable : row.state;
  return { color, label, canMerge };
}

// A goal is "waiting on you" when it's blocked or its direction says needs_human.
export function needsYou(phase: string | null, verdict?: Verdict | null): boolean {
  return phase === "blocked" || verdict === "needs_human";
}
