"""Shared goal-layer test doubles — no network, no claude, deterministic clock."""

from __future__ import annotations

from datetime import datetime, timezone

from devclaw.goal.models import Action, Goal, InFlight, PollResult
from devclaw.loom import trace as _trace


class FakeClaude:
    """A claude_caller that returns a canned response and counts calls.

    The call count IS the quota assertion — an idle tick must leave it at 0.
    Used for both the planner caller and the evaluator caller. Records into the
    active tracer (if one is set) under the given ``role`` so the trace harness
    sees the same shape it would in live mode.
    """

    def __init__(self, response: str = "{}", *, role: str = "fake") -> None:
        self.response = response
        self.calls = 0
        self.last_prompt = ""
        self.role = role

    async def __call__(self, prompt: str) -> str:
        self.calls += 1
        self.last_prompt = prompt
        _trace.record_cognition(
            role=self.role, model="(stub)", prompt=prompt, response=self.response, latency_ms=0,
        )
        return self.response


class FakeEngine:
    """In-process engine double — records dispatches, returns canned polls."""

    def __init__(self, poll_result: PollResult | None = None, dispatch_ref: InFlight | None = None) -> None:
        self.poll_result = poll_result
        self.dispatch_ref = dispatch_ref
        self.dispatched: list[tuple[Action, Goal, str]] = []
        self.polls = 0

    async def dispatch(self, action: Action, goal: Goal, notify_url: str) -> InFlight:
        self.dispatched.append((action, goal, notify_url))
        ref_kind = "program" if action.tool == "start_program" else "task"
        return self.dispatch_ref or InFlight("devclaw", action.tool, "task_x", ref_kind, action.goal)

    async def poll(self, ref: InFlight) -> PollResult:
        self.polls += 1
        assert self.poll_result is not None, "poll called but no poll_result configured"
        return self.poll_result


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


async def fake_prepare(
    workspace_dir: str, repo_url: str | None = None, branch: str | None = None,
    skills_required: list[str] | None = None,
) -> str:
    """No-op workspace prep for tick tests. Returns the requested branch when
    one is passed (mirrors the real prepare_workspace return for goal-branch
    mode) so a settle-side assertion can check what branch was prepped.
    ``skills_required`` accepted for protocol parity; the fake never provisions."""
    return branch or "main"


class Clock:
    """Injectable, advanceable clock."""

    def __init__(self, t: datetime | None = None) -> None:
        self.t = t or datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self.t = self.t + timedelta(seconds=seconds)


def seed_goal(
    goals_dir, goal_id: str = "demo", *, cadence: str = "1d", backlog: list[str] | None = None
) -> None:
    """Write a minimal goal.yaml under goals_dir/<goal_id>/."""
    import yaml

    d = goals_dir / goal_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "goal.yaml").write_text(
        yaml.safe_dump(
            {
                "objective": "Drive the demo repo to done.",
                "cadence": cadence,
                "engine": "devclaw",
                "workspace_dir": "/repos/demo",
                "verify_cmd": "pytest -q",
                "open_pr": True,
                "done_when": "all backlog items merged",
                "backlog": backlog or ["add a /health endpoint", "add request logging"],
            }
        )
    )
