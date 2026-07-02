"""L8 scorecard telemetry — the rolling merge / steer / first-pass counters
plan.md §Measurement direction calls out as the "PR-by-PR delta on the scorecard
signals" surface.

Two ways in:

- ``compute_scorecard(store, window_hours=168)`` — a pure function over the
  state_store's ``tasks`` and ``traces`` tables. Cheap SQL + a light Python
  pass over cognition ``response_preview`` strings; no cognition call.
- ``devclaw scorecard`` (CLI) and the ``get_scorecard_metrics`` MCP tool
  wrap the same function.

Deliberately *narrow* v1: skips the VPS-side dashboard render (separate infra)
and stays out of every path the goal engine actually runs — reading only. If a
metric can't be computed exactly from what the state store carries today, we
prefer a best-effort estimate with an explicit ``estimate_notes`` field over
inventing new persistence.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

# ``role`` labels used by the cognition tracer for evaluator calls. See the
# ``role=`` arg on ``claude_with_model`` in ``goal/evaluator.py`` — the tracer
# stamps this into every CognitionEvent, so verdict counts can be scoped to
# the evaluator only.
_EVALUATOR_ROLE = "evaluator"

#: verdicts the evaluator can emit. See ``goal/evaluator._VALID_VERDICTS``;
#: reproduced here to avoid importing the evaluator (and its Anthropic caller)
#: into the telemetry module — telemetry stays a pure, dependency-light path.
_EVAL_VERDICTS = ("on_track", "off_track", "achieved", "stalled", "needs_human")

#: Best-effort verdict extractor. The tracer stores the model response as a
#: 240-char ``response_preview``; that's plenty to see the JSON prefix a
#: well-behaved evaluator returns. If the field can't be parsed (truncation,
#: model returned prose, error string), the row lands under ``unparseable`` —
#: a signal in its own right about model output quality.
_VERDICT_RE = re.compile(r'"verdict"\s*:\s*"(\w+)"')


def _now_ms() -> int:
    return int(time.time() * 1000)


def _extract_verdict(preview: str) -> Optional[str]:
    """Pull the verdict string out of an evaluator ``response_preview``.
    Returns None when the preview doesn't look like an evaluator response
    (which happens for planner/decomposer roles too — the caller filters
    by role first, but this stays defensive)."""
    if not preview:
        return None
    m = _VERDICT_RE.search(preview)
    if not m:
        return None
    v = m.group(1).strip().lower()
    return v if v in _EVAL_VERDICTS else None


def compute_scorecard(store: Any, *, window_hours: int = 168) -> dict:
    """Roll up L8 scorecard metrics over the last ``window_hours``.

    ``store`` is a ``devclaw.state_store.StateStore`` — typed as ``Any`` here
    to keep the telemetry module import-light (no circular pull-in with the
    goal layer). Everything read is via public methods on the store: tasks
    counts + a raw cursor over ``traces`` for evaluator calls.
    """
    since_ms = _now_ms() - int(window_hours * 3600 * 1000)

    # ---- tasks + merge rate --------------------------------------------
    with store._lock:  # noqa: SLF001 — telemetry co-designs with state_store
        by_status = dict(
            store._db.execute(
                "SELECT status, COUNT(*) AS n FROM tasks "
                "WHERE completed_at IS NOT NULL AND completed_at >= ? "
                "GROUP BY status",
                (since_ms,),
            ).fetchall()
        )
        merged_with_pr_row = store._db.execute(
            "SELECT COUNT(*) AS n FROM tasks "
            "WHERE status = 'done' AND pr_url IS NOT NULL AND pr_url != '' "
            "AND completed_at IS NOT NULL AND completed_at >= ?",
            (since_ms,),
        ).fetchone()
        merged_with_pr = int(merged_with_pr_row["n"] if merged_with_pr_row else 0)

        # ---- workspace breaks tripped in window ------------------------
        breaks_row = store._db.execute(
            "SELECT COUNT(*) AS n FROM events "
            "WHERE type = 'workspace_break_tripped' AND ts >= ?",
            (since_ms,),
        ).fetchone()
        workspace_breaks = int(breaks_row["n"] if breaks_row else 0)

        # ---- evaluator calls + verdict distribution --------------------
        # Traces don't index by role; a small window means a full scan is fine.
        # Filter to kind='cognition' at SQL level, then to role='evaluator' in
        # Python (role lives inside payload_json — no dedicated column).
        cog_rows = store._db.execute(
            "SELECT payload_json FROM traces "
            "WHERE kind = 'cognition' AND ts >= ? "
            "ORDER BY id ASC",
            (since_ms,),
        ).fetchall()

    verdicts: dict[str, int] = {v: 0 for v in _EVAL_VERDICTS}
    unparseable = 0
    eval_calls = 0
    for r in cog_rows:
        try:
            p = json.loads(r["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if p.get("role") != _EVALUATOR_ROLE:
            continue
        eval_calls += 1
        v = _extract_verdict(p.get("response_preview") or "")
        if v is None:
            unparseable += 1
            continue
        verdicts[v] += 1

    total_terminal = int(sum(by_status.values()))
    done_count = int(by_status.get("done", 0))
    failed_count = int(by_status.get("failed", 0))
    cancelled_count = int(by_status.get("cancelled", 0))

    merge_rate = (merged_with_pr / done_count) if done_count else 0.0
    # Steer rate: of evaluator verdicts that landed cleanly, what fraction were
    # off_track (each off_track writes corrections to inbox.md → the planner
    # picks them up next tick, i.e. one implicit steer). A rough but honest
    # proxy for "how often the loop needed correction to stay on track."
    classified = sum(verdicts.values())
    steer_rate = (verdicts["off_track"] / classified) if classified else 0.0
    # First-pass hit rate: of classified evaluator verdicts, what fraction
    # were `achieved` — a coarse cousin of "done-gate first-pass hit rate."
    # Coarse because the trace doesn't separate done-gate calls from
    # progress-check calls; both flow through the same evaluator role. If
    # done-gate calls dominate the achieved bucket (they usually do — the
    # progress-check verdict is typically on_track), this is a reasonable
    # first cut. Tightening it needs a `at_done_gate` flag on the cognition
    # trace record — a small state_store change, out of L8-v1 scope.
    first_pass_hit_rate = (verdicts["achieved"] / classified) if classified else 0.0

    return {
        "window_hours": window_hours,
        "since_ms": since_ms,
        "computed_at_ms": _now_ms(),
        "tasks": {
            "total_terminal": total_terminal,
            "done": done_count,
            "failed": failed_count,
            "cancelled": cancelled_count,
            "merged_with_pr": merged_with_pr,
        },
        "merge_rate": round(merge_rate, 4),
        "workspace_breaks_tripped": workspace_breaks,
        "evaluator": {
            "total_calls": eval_calls,
            "verdicts": verdicts,
            "unparseable_responses": unparseable,
            "steer_rate": round(steer_rate, 4),
            "first_pass_hit_rate": round(first_pass_hit_rate, 4),
        },
        "estimate_notes": [
            "first_pass_hit_rate mixes done-gate + progress-check evaluator "
            "calls; a dedicated at_done_gate flag on cognition trace records "
            "would tighten this — see plan.md §Measurement direction.",
            "steer_rate uses off_track verdicts as a steering proxy; owner-"
            "written inbox.md steers are not (yet) traced separately.",
        ],
    }


def format_scorecard(sc: dict) -> str:
    """Render a scorecard dict for human eyeballing on the terminal."""
    t = sc["tasks"]
    e = sc["evaluator"]
    lines = [
        f"window:           last {sc['window_hours']}h",
        f"tasks (terminal): {t['total_terminal']} "
        f"(done {t['done']}, failed {t['failed']}, cancelled {t['cancelled']})",
        f"merged with PR:   {t['merged_with_pr']}  →  merge rate {sc['merge_rate'] * 100:.1f}%",
        f"workspace breaks: {sc['workspace_breaks_tripped']}",
        f"evaluator calls:  {e['total_calls']}  (unparseable {e['unparseable_responses']})",
        "verdicts:",
    ]
    for v in _EVAL_VERDICTS:
        lines.append(f"  {v:<14} {e['verdicts'][v]}")
    lines.append(f"steer rate:       {e['steer_rate'] * 100:.1f}%   (off_track / classified)")
    lines.append(f"first-pass hit:   {e['first_pass_hit_rate'] * 100:.1f}%   (achieved / classified)")
    lines.append("")
    lines.append("estimate notes:")
    for n in sc.get("estimate_notes") or []:
        lines.append(f"  - {n}")
    return "\n".join(lines)
