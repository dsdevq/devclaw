"""Eval scoring — pure functions for grading a build-from-scratch run.

The live runner (``evals/run.py``) drives the real pipeline and feeds the
``get_program`` output + an acceptance result into :func:`score`; this module
holds the deterministic scoring/aggregation so it's unit-testable without a
server, docker, or claude. A single run is noisy — :func:`aggregate` turns N
runs into the rate/averages that actually measure progress.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class Scorecard:
    run: int
    program_id: Optional[str]
    program_status: str  # planning | running | done | failed | (timeout)
    #: did the acceptance check pass? None when the build never finished so it
    #: wasn't run.
    acceptance_passed: Optional[bool]
    tasks_total: int
    tasks_done: int
    tasks_failed: int
    milestone_total: int
    milestone_done: int
    milestone_pct: float
    wall_ms: int
    #: the run made no progress within the timeout (no task settled)
    stuck: bool

    def to_dict(self) -> dict:
        return asdict(self)


def score(
    *,
    run: int,
    program: dict,
    tasks: list[dict],
    acceptance_passed: Optional[bool],
    wall_ms: int,
    stuck: bool = False,
) -> Scorecard:
    """Grade one run from the ``get_program`` shape ({program, tasks}). A
    milestone counts 'done' only when ALL its tasks are done — partial credit at
    the milestone granularity. With no milestones (a plain program), milestone_pct
    falls back to task-completion percent."""
    statuses = [t.get("status", "") for t in tasks]
    tasks_total = len(tasks)
    tasks_done = statuses.count("done")
    tasks_failed = statuses.count("failed")

    by_ms: dict[str, list[str]] = {}
    for t in tasks:
        ms = t.get("milestone")
        if ms:
            by_ms.setdefault(ms, []).append(t.get("status", ""))
    milestone_total = len(by_ms)
    milestone_done = sum(1 for sts in by_ms.values() if sts and all(s == "done" for s in sts))

    if milestone_total:
        milestone_pct = round(100.0 * milestone_done / milestone_total, 1)
    elif tasks_total:
        milestone_pct = round(100.0 * tasks_done / tasks_total, 1)
    else:
        milestone_pct = 0.0

    return Scorecard(
        run=run,
        program_id=program.get("id"),
        program_status=program.get("status", "unknown"),
        acceptance_passed=acceptance_passed,
        tasks_total=tasks_total,
        tasks_done=tasks_done,
        tasks_failed=tasks_failed,
        milestone_total=milestone_total,
        milestone_done=milestone_done,
        milestone_pct=milestone_pct,
        wall_ms=wall_ms,
        stuck=stuck,
    )


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 1) if xs else 0.0


def aggregate(cards: list[Scorecard]) -> dict:
    """Roll N scorecards into the headline metrics. The acceptance *pass rate* is
    the success metric; the rest say how/why it's improving."""
    n = len(cards)
    passed = sum(1 for c in cards if c.acceptance_passed is True)
    completed = sum(1 for c in cards if c.program_status == "done")
    return {
        "runs": n,
        "acceptance_passed": passed,
        "acceptance_pass_rate": round(passed / n, 3) if n else 0.0,
        "builds_completed": completed,
        "build_completion_rate": round(completed / n, 3) if n else 0.0,
        "avg_milestone_pct": _mean([c.milestone_pct for c in cards]),
        "avg_tasks_done": _mean([float(c.tasks_done) for c in cards]),
        "avg_tasks_failed": _mean([float(c.tasks_failed) for c in cards]),
        "avg_wall_ms": int(_mean([float(c.wall_ms) for c in cards])),
        "stuck_runs": sum(1 for c in cards if c.stuck),
    }


def next_answer(scripted: list[str], turn_idx: int) -> str:
    """The grill answer for turn ``turn_idx`` (0-based). Uses the scripted answer
    if present, else defers to DevClaw's own recommendation — so a fixed script
    can be short and the spec still converges."""
    if 0 <= turn_idx < len(scripted):
        return scripted[turn_idx]
    return "Use your recommended answer."
