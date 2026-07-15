"""L8 scorecard telemetry — the rolling merge / steer / first-pass counters
plan.md §Measurement direction calls out as the "PR-by-PR delta on the scorecard
signals" surface.

Two ways in:

- ``compute_scorecard(store, window_hours=168)`` — a pure function over the
  state_store's ``tasks`` and ``traces`` tables. Cheap SQL + a light Python
  pass over cognition response text (full ``response_text`` since T0.5,
  ``response_preview`` for legacy rows); no cognition call.
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
import math
import re
import time
from datetime import datetime, timezone
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

#: Best-effort verdict extractor. Since T0.5 the tracer stores the FULL model
#: response as ``response_text``, so the verdict is found wherever it sits in
#: the response; legacy rows carry only the 240-char ``response_preview`` and
#: fall back to that. If neither yields a verdict (model returned prose, error
#: string, or a legacy preview truncated mid-JSON), the row lands under
#: ``unparseable`` — going forward that bucket should only contain genuinely
#: verdict-less responses.
_VERDICT_RE = re.compile(r'"verdict"\s*:\s*"(\w+)"')

#: Same shape for the structural axis. Present only at done-gate responses;
#: absent from progress-check calls. Missing values are counted as ``"unknown"``
#: so the dashboard can spot legacy / non-done-gate calls without them polluting
#: the concrete grades.
_STRUCTURAL_RE = re.compile(r'"structural_health"\s*:\s*"(\w+)"')
_STRUCTURAL_GRADES = ("clean", "concerns", "poor")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _extract_verdict(preview: str) -> Optional[str]:
    """Pull the verdict string out of an evaluator response (the full
    ``response_text`` since T0.5; the 240-char ``response_preview`` for legacy
    rows). Returns None when the text doesn't look like an evaluator response
    (which happens for planner/decomposer roles too — the caller filters
    by role first, but this stays defensive)."""
    if not preview:
        return None
    m = _VERDICT_RE.search(preview)
    if not m:
        return None
    v = m.group(1).strip().lower()
    return v if v in _EVAL_VERDICTS else None


def _extract_structural(preview: str) -> Optional[str]:
    """Pull the ``structural_health`` grade — the axis-B verdict added by C3.
    Returns None when the field is absent (progress-check or legacy call);
    only recognized values pass through."""
    if not preview:
        return None
    m = _STRUCTURAL_RE.search(preview)
    if not m:
        return None
    g = m.group(1).strip().lower()
    return g if g in _STRUCTURAL_GRADES else None


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
    structural: dict[str, int] = {g: 0 for g in _STRUCTURAL_GRADES}
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
        # T0.5: prefer the full response text; legacy rows only have the
        # 240-char preview (verdicts past the truncation were "unparseable").
        text = p.get("response_text") or p.get("response_preview") or ""
        v = _extract_verdict(text)
        if v is None:
            unparseable += 1
            continue
        verdicts[v] += 1
        # Structural grade is present only at done-gate responses. Absent
        # elsewhere — don't inflate the denominator by counting misses.
        g = _extract_structural(text)
        if g is not None:
            structural[g] += 1

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
            # Axis-B distribution — only counted for responses that carried a
            # structural_health field (post-C3 done-gate calls). Empty when no
            # evaluator response in-window reported one.
            "structural_grades": structural,
        },
        "estimate_notes": [
            "first_pass_hit_rate mixes done-gate + progress-check evaluator "
            "calls; a dedicated at_done_gate flag on cognition trace records "
            "would tighten this — see plan.md §Measurement direction.",
            "steer_rate uses off_track verdicts as a steering proxy; owner-"
            "written inbox.md steers are not (yet) traced separately.",
        ],
    }


# ---- trace read surface (day-report + shared --since parsing) --------------
#
# Same philosophy as the scorecard above: pure SQL + a light Python pass over
# the SQL-narrowed rows, NO cognition call anywhere. The production traces
# table holds 200k+ rows — every query below filters in SQL first (kind/ts ride
# their indexes); Python only ever touches the in-window subset.


_SINCE_RE = re.compile(r"^(\d+)([mhd])$")
_SINCE_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}


def parse_since(spec: str, *, now_ms: Optional[int] = None) -> int:
    """Parse a ``--since`` spec into an epoch-ms lower bound.

    Accepts a relative window (``30m`` / ``24h`` / ``7d``) or an ISO-8601
    timestamp (naive → UTC, matching the epoch-ms ``ts`` the tracer writes).
    Raises ``ValueError`` on anything else — the CLI/HTTP callers turn that
    into a usage error instead of silently reading the whole table."""
    s = (spec or "").strip()
    m = _SINCE_RE.match(s)
    if m:
        base = now_ms if now_ms is not None else _now_ms()
        return base - int(m.group(1)) * _SINCE_UNIT_MS[m.group(2)]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(
            f"bad --since {spec!r}: use <N>m/<N>h/<N>d or an ISO timestamp"
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _error_class(error: str) -> str:
    """Deterministic error bucket: 'timeout' outranks everything (that's the
    class the owner greps for first), else the first line's prefix before the
    first ':' — devclaw error strings lead with their class ("spawn failed:",
    "review gate crashed:", "delivery failed:", ...)."""
    e = (error or "").strip()
    if not e:
        return "(none)"
    low = e.lower()
    if "timeout" in low or "timed out" in low:
        return "timeout"
    head = e.splitlines()[0].split(":", 1)[0].strip()
    return (head[:60] or "(unclassified)").lower()


def _percentile(sorted_values: list[int], q: float) -> int:
    """Nearest-rank percentile over an ascending list. Deterministic, no
    interpolation — report numbers must be reproducible byte-for-byte."""
    if not sorted_values:
        return 0
    rank = max(1, math.ceil(q * len(sorted_values)))
    return int(sorted_values[min(rank, len(sorted_values)) - 1])


def compute_trace_report(store: Any, *, since_ms: int) -> dict:
    """The 'what happened overnight' day-report: deterministic aggregates over
    ``tasks`` + ``traces`` since ``since_ms``. Reads only; NO LLM.

    Sections: tasks dispatched/settled by status + failed-task error classes,
    cognition calls by role (count / p50 / p90 / max latency, timeouts), retry
    storms (same task title attempted more than once), OWNER notifications,
    trend_check volume."""
    with store._lock:  # noqa: SLF001 — telemetry co-designs with state_store
        dispatched_row = store._db.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE created_at >= ?",
            (since_ms,),
        ).fetchone()
        settled_by_status = dict(
            store._db.execute(
                "SELECT status, COUNT(*) AS n FROM tasks "
                "WHERE completed_at IS NOT NULL AND completed_at >= ? "
                "GROUP BY status",
                (since_ms,),
            ).fetchall()
        )
        failed_rows = store._db.execute(
            "SELECT error FROM tasks "
            "WHERE status = 'failed' AND completed_at IS NOT NULL "
            "AND completed_at >= ?",
            (since_ms,),
        ).fetchall()
        storm_rows = store._db.execute(
            "SELECT title, COUNT(*) AS n FROM tasks "
            "WHERE created_at >= ? AND title IS NOT NULL AND title != '' "
            "GROUP BY title HAVING n > 1 ORDER BY n DESC, title ASC",
            (since_ms,),
        ).fetchall()
        cog_rows = store._db.execute(
            "SELECT payload_json FROM traces "
            "WHERE kind = 'cognition' AND ts >= ? ORDER BY id ASC",
            (since_ms,),
        ).fetchall()
        notify_rows = store._db.execute(
            "SELECT COALESCE(json_extract(payload_json, '$.level'), '') AS lvl, "
            "COUNT(*) AS n FROM traces "
            "WHERE kind = 'notify' AND ts >= ? GROUP BY lvl",
            (since_ms,),
        ).fetchall()
        trend_total_row = store._db.execute(
            "SELECT COUNT(*) AS n FROM traces "
            "WHERE kind = 'trend_check' AND ts >= ?",
            (since_ms,),
        ).fetchone()
        trend_fired_row = store._db.execute(
            "SELECT COUNT(*) AS n FROM traces "
            "WHERE kind = 'trend_check' AND ts >= ? "
            "AND json_extract(payload_json, '$.fired')",
            (since_ms,),
        ).fetchone()

    error_classes: dict[str, int] = {}
    for r in failed_rows:
        c = _error_class(r["error"] or "")
        error_classes[c] = error_classes.get(c, 0) + 1

    # Cognition by role — the role lives inside payload_json; the SQL above
    # already narrowed to in-window cognition rows, so this pass is bounded.
    by_role: dict[str, dict] = {}
    latencies: dict[str, list[int]] = {}
    for r in cog_rows:
        try:
            p = json.loads(r["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        role = str(p.get("role") or "(unknown)")
        rec = by_role.setdefault(
            role, {"calls": 0, "errors": 0, "timeouts": 0},
        )
        rec["calls"] += 1
        err = str(p.get("error") or "")
        if err:
            rec["errors"] += 1
            if _error_class(err) == "timeout":
                rec["timeouts"] += 1
        latencies.setdefault(role, []).append(int(p.get("latency_ms") or 0))
    for role, vals in latencies.items():
        vals.sort()
        by_role[role]["latency_ms"] = {
            "p50": _percentile(vals, 0.50),
            "p90": _percentile(vals, 0.90),
            "max": vals[-1] if vals else 0,
        }

    notify_by_level = {str(r["lvl"] or "(unknown)"): int(r["n"]) for r in notify_rows}

    return {
        "since_ms": since_ms,
        "computed_at_ms": _now_ms(),
        "tasks": {
            "dispatched": int(dispatched_row["n"] if dispatched_row else 0),
            "settled_by_status": {k: int(v) for k, v in sorted(settled_by_status.items())},
            "failed_error_classes": dict(sorted(error_classes.items())),
        },
        "cognition": {
            "total_calls": sum(r["calls"] for r in by_role.values()),
            "by_role": {k: by_role[k] for k in sorted(by_role)},
        },
        "retry_storms": [
            {"title": str(r["title"]), "attempts": int(r["n"])} for r in storm_rows
        ],
        "notifications": {
            "owner": int(notify_by_level.get("OWNER", 0)),
            "by_level": notify_by_level,
        },
        "trend_checks": {
            "total": int(trend_total_row["n"] if trend_total_row else 0),
            "fired": int(trend_fired_row["n"] if trend_fired_row else 0),
        },
    }


def format_trace_report(rep: dict) -> str:
    """Render a trace report dict for human eyeballing on the terminal."""
    def _iso(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(
            timespec="seconds"
        )

    t = rep["tasks"]
    lines = [
        f"window:      since {_iso(rep['since_ms'])}",
        f"tasks:       dispatched {t['dispatched']}",
    ]
    settled = t["settled_by_status"]
    if settled:
        lines.append(
            "  settled:   "
            + ", ".join(f"{k} {v}" for k, v in settled.items())
        )
    else:
        lines.append("  settled:   (none)")
    if t["failed_error_classes"]:
        lines.append("  failed by error class:")
        for cls, n in t["failed_error_classes"].items():
            lines.append(f"    {cls:<28} {n}")
    c = rep["cognition"]
    lines.append(f"cognition:   {c['total_calls']} calls")
    for role, rec in c["by_role"].items():
        lat = rec.get("latency_ms") or {}
        lines.append(
            f"  {role:<12} calls {rec['calls']:<4} "
            f"p50 {lat.get('p50', 0)}ms  p90 {lat.get('p90', 0)}ms  "
            f"max {lat.get('max', 0)}ms  timeouts {rec['timeouts']}"
        )
    storms = rep["retry_storms"]
    if storms:
        lines.append("retry storms (same title attempted >1):")
        for s in storms:
            lines.append(f"  {s['attempts']}x  {s['title']}")
    else:
        lines.append("retry storms: (none)")
    lines.append(f"notify:      OWNER {rep['notifications']['owner']}")
    for lvl, n in sorted(rep["notifications"]["by_level"].items()):
        if lvl != "OWNER":
            lines.append(f"  {lvl:<12} {n}")
    tc = rep["trend_checks"]
    lines.append(f"trend checks: {tc['total']} ({tc['fired']} fired)")
    return "\n".join(lines)


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
    struct = e.get("structural_grades") or {}
    if any(struct.values()):
        lines.append("structural (done-gate only):")
        for g in _STRUCTURAL_GRADES:
            lines.append(f"  {g:<14} {struct.get(g, 0)}")
    lines.append("")
    lines.append("estimate notes:")
    for n in sc.get("estimate_notes") or []:
        lines.append(f"  - {n}")
    return "\n".join(lines)
