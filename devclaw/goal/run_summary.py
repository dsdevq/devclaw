"""RUN_SUMMARY.md — the at-a-glance close-out artifact for a finished goal.

A goal that closes ``achieved`` leaves behind a single readable record of what
the run actually did: deliveries and their PRs, gate verdicts, total diff
volume (per-delivery stats captured at settle since the DeliveryEvent gained
them), real token/cost totals from the cognition traces, and wall-clock
duration from the phase history. Everything here is a **projection of
existing rows** (traces + phase_history + checklist) — computed at the
ACHIEVE close, written as a generated view alongside STATUS.md/log.md/
deliveries.md, and never read back for decisions.

Pure module: :func:`build_run_summary` takes the rows and returns
``(markdown, compact_line)``; the caller (tick_donegate) collects inputs and
persists. Tolerant by design — missing/malformed payload fields degrade to
omission, never an exception (a summary hiccup must not disturb a verified
close; the caller additionally wraps the whole step best-effort).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .models import Checklist, GoalStatus


def _parse_iso_ms(ts: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
    except (ValueError, TypeError, AttributeError):
        return None


def _fmt_duration(ms: float) -> str:
    minutes = int(ms // 60_000)
    if minutes < 60:
        return f"{max(minutes, 1)}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _int_or_none(v: object) -> Optional[int]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def build_run_summary(
    goal_id: str,
    status: GoalStatus,
    traces: list[dict],
    totals: Optional[dict] = None,
    checklist: Optional[Checklist] = None,
    objective: str = "",
) -> tuple[str, str]:
    """Render the summary from rows. Returns ``(markdown, compact_line)`` —
    the markdown becomes RUN_SUMMARY.md; the compact line rides the owner's
    goal-complete notification.

    ``traces`` is the goal's trace rows (``read_traces`` shape: dicts with a
    parsed ``payload``); only ``kind == "delivery"`` rows are consumed here.
    ``totals`` is ``StateStore.trace_totals``' dict (cognition token/cost
    aggregates); None/empty degrades to omitting the cognition line.
    """
    deliveries: list[dict] = []
    for t in traces:
        if not isinstance(t, dict) or t.get("kind") != "delivery":
            continue
        p = t.get("payload")
        if isinstance(p, dict):
            deliveries.append(p)

    prs: list[str] = []
    gates_passed = gates_failed = 0
    files = insertions = deletions = 0
    have_diff = False
    for d in deliveries:
        url = d.get("pr_url")
        if isinstance(url, str) and url and url not in prs:
            prs.append(url)
        gp = d.get("gate_passed")
        if gp is True:
            gates_passed += 1
        elif gp is False:
            gates_failed += 1
        f = _int_or_none(d.get("diff_files"))
        i = _int_or_none(d.get("diff_insertions"))
        x = _int_or_none(d.get("diff_deletions"))
        if f is not None or i is not None or x is not None:
            have_diff = True
            files += f or 0
            insertions += i or 0
            deletions += x or 0

    # wall clock: first phase_history entry → the newest (the done entry)
    duration = ""
    history = list(status.phase_history or [])
    if len(history) >= 2:
        first = _parse_iso_ms(str(history[0].get("at", "")))
        last = _parse_iso_ms(str(history[-1].get("at", "")))
        if first is not None and last is not None and last > first:
            duration = _fmt_duration(last - first)

    items_line = ""
    if checklist is not None and checklist.items:
        done = sum(1 for i in checklist.items if i.status == "done")
        blocked = sum(1 for i in checklist.items if i.status == "blocked")
        items_line = f"{done}/{len(checklist.items)} checklist items done"
        if blocked:
            items_line += f" ({blocked} blocked)"

    tokens_line = ""
    if isinstance(totals, dict):
        tok = (totals.get("cognition_tokens_in") or 0) + (
            totals.get("cognition_tokens_out") or 0
        )
        cost = totals.get("cognition_cost_usd")
        calls = (totals.get("events_by_kind") or {}).get("cognition")
        if tok:
            tokens_line = f"{_fmt_tokens(int(tok))} tokens"
            if isinstance(cost, (int, float)) and cost:
                tokens_line += f" (${cost:.2f})"
            if isinstance(calls, int) and calls:
                tokens_line += f" over {calls} cognition calls"

    # ---- compact line (rides the owner notify) ------------------------------
    bits: list[str] = [f"{len(deliveries)} deliveries"]
    if prs:
        bits.append(f"{len(prs)} PR{'s' if len(prs) != 1 else ''}")
    if have_diff:
        bits.append(f"+{insertions}/-{deletions} across {files} files")
    if tokens_line:
        bits.append(tokens_line)
    if duration:
        bits.append(duration)
    compact = " · ".join(bits)

    # ---- markdown -----------------------------------------------------------
    lines: list[str] = [f"# {goal_id} — run summary", ""]
    if objective.strip():
        lines += [f"**Objective:** {objective.strip()}", ""]
    lines.append(f"- **Deliveries:** {len(deliveries)}"
                 + (f" ({gates_passed} gate-passed, {gates_failed} gate-failed)"
                    if (gates_passed or gates_failed) else ""))
    if items_line:
        lines.append(f"- **Progress:** {items_line}")
    if have_diff:
        lines.append(
            f"- **Diff volume:** +{insertions}/-{deletions} across {files} files"
        )
    if tokens_line:
        lines.append(f"- **Cognition:** {tokens_line}")
    if duration:
        lines.append(f"- **Duration:** {duration}")
    if status.last_eval_note:
        lines.append(f"- **Close verdict:** {status.last_eval_note}")
    if prs:
        lines += ["", "## Pull requests", ""]
        lines += [f"- {u}" for u in prs]
    if deliveries:
        lines += ["", "## Deliveries", ""]
        for d in deliveries:
            label = str(d.get("action_label") or "delivery")
            gp = d.get("gate_passed")
            gate = "passed" if gp is True else ("FAILED" if gp is False else "—")
            url = d.get("pr_url") or ""
            lines.append(f"- {label} · gate={gate}" + (f" · {url}" if url else ""))
    lines += [
        "",
        "_Generated view — a projection of the goal's trace/phase rows at the "
        "ACHIEVE close; never read back for decisions._",
        "",
    ]
    return "\n".join(lines), compact
