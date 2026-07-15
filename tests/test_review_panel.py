"""Review PANEL — the durable, stateful analog of an ephemeral adversarial
fan-out (opt-in via DEVCLAW_REVIEW_PANEL_N).

Two halves, mirroring test_review_gate.py:
  1. the pure module (devclaw/quality): panel aggregation, diverse-lens prompts,
     fail-CLOSED-on-sub-quorum, vote persistence.
  2. the queue integration: N=1 is byte-identical to the single reviewer; a panel
     vote lands as an append-only ``review_vote`` event.

Driven with a stubbed panelist caller (no docker, no claude) — the panel NEVER
calls a real model here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from devclaw import quality as review_gate
from devclaw import task_queue
from devclaw.engine import EngineRequest
from devclaw.planner import PlannerError
from devclaw.quality import (
    _REVIEW_LENSES,
    build_review_prompt,
    review_diff,
    review_panel,
)
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue

_DIFF = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -0,0 +1 @@\n+code\n"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _approve_json(summary: str = "ok") -> str:
    return '{"verdict": "approve", "summary": "%s", "issues": []}' % summary


def _blocker_json(location: str, problem: str) -> str:
    return (
        '{"verdict": "request_changes", "summary": "found a defect", "issues": ['
        '{"severity": "blocker", "location": "%s", "problem": "%s", "fix": "fix it"}]}'
        % (location, problem)
    )


def _caller_returning(*responses: str):
    """A panelist caller that returns the next canned response per invocation
    (round-robins through the lens fan-out in order)."""
    seq = list(responses)
    idx = {"i": 0}

    async def caller(prompt: str) -> str:
        r = seq[idx["i"] % len(seq)]
        idx["i"] += 1
        return r

    return caller


# ============================ pure module: lenses ====================

def test_lens_prompt_carries_its_own_lens_and_not_the_others():
    """A per-lens prompt names ITS lens focus and no other lens's — the diverse
    lenses are what make the panelists uncorrelated. Presence AND absence, proven
    against the raw template (which carries no PANEL LENS marker at all)."""
    raw = Path(review_gate.__file__).parent.parent / "prompts" / "review-gate.md"
    template = raw.read_text(encoding="utf-8")
    assert "PANEL LENS" not in template  # the marker is added by the lens, not canned

    p_corr = build_review_prompt(goal="g", kind="implement_feature", diff="d", lens="correctness")
    assert "PANEL LENS — CORRECTNESS" in p_corr
    assert "PANEL LENS — REGRESSION RISK" not in p_corr
    assert "PANEL LENS — ACCEPTANCE CRITERIA" not in p_corr

    p_reg = build_review_prompt(goal="g", kind="implement_feature", diff="d", lens="regression_risk")
    assert "PANEL LENS — REGRESSION RISK" in p_reg
    assert "PANEL LENS — CORRECTNESS" not in p_reg

    p_ac = build_review_prompt(goal="g", kind="implement_feature", diff="d", lens="meets_acceptance_criteria")
    assert "PANEL LENS — ACCEPTANCE CRITERIA" in p_ac
    assert "acceptance criteria" in p_ac.lower()


def test_no_lens_prompt_is_byte_identical_to_single_reviewer():
    """lens=None (the single-reviewer / N==1 path) produces the SAME prompt as
    before the panel existed — no PANEL LENS block leaks in."""
    p = build_review_prompt(goal="g", kind="implement_feature", diff="d")
    p_none = build_review_prompt(goal="g", kind="implement_feature", diff="d", lens=None)
    assert p == p_none
    assert "PANEL LENS" not in p


def test_every_lens_prompt_keeps_the_grounding_clause():
    """The #227 grounding clause (REPOSITORY CONTEXT is authoritative; do NOT
    infer repo facts from elsewhere) must survive in EVERY lens — a lens only
    ADDS focus, it never strips the anti-inference guard."""
    for lens in _REVIEW_LENSES:
        p = build_review_prompt(
            goal="g", kind="implement_feature", diff="d", lens=lens,
            repo_context="git_remote_origin: https://example/x.git",
        )
        assert "Do NOT infer repository facts" in p
        assert "REPOSITORY CONTEXT" in p


# ============================ pure module: aggregation ================

async def test_n1_verdict_identical_to_review_diff():
    """N=1 delegates straight to review_diff → identical verdict dict on the same
    input. The default keeps the gate byte-identical to today until an operator
    opts in."""
    caller = _caller_returning(_blocker_json("f.py", "off-by-one"))
    single = await review_diff(goal="g", kind="implement_feature", diff=_DIFF, claude_caller=caller)
    panel = await review_panel(goal="g", kind="implement_feature", diff=_DIFF, claude_caller=caller, n=1)
    assert panel == single
    assert panel["verdict"] == "request_changes"


async def test_union_any_single_blocker_requests_changes():
    """Evidence-wins union: two lenses approve, one flags a blocker → the panel
    requests changes. The panel is strictly >= as strict as one reviewer."""
    # lens order is (correctness, regression_risk, meets_acceptance_criteria);
    # only the 2nd panelist finds a defect.
    caller = _caller_returning(
        _approve_json("clean"),
        _blocker_json("svc.py:42", "breaks a shared contract"),
        _approve_json("meets AC"),
    )
    result = await review_panel(goal="g", kind="implement_feature", diff=_DIFF, claude_caller=caller, n=3)
    assert result["verdict"] == "request_changes"
    assert len(result["blocking"]) == 1
    assert result["blocking"][0]["location"] == "svc.py:42"


async def test_union_all_approve_approves():
    caller = _caller_returning(_approve_json(), _approve_json(), _approve_json())
    result = await review_panel(goal="g", kind="implement_feature", diff=_DIFF, claude_caller=caller, n=3)
    assert result["verdict"] == "approve"
    assert result["blocking"] == []


async def test_union_dedups_same_location_and_severity():
    """Two panelists flag the SAME (location, severity) — the union carries it
    once, not twice."""
    caller = _caller_returning(
        _blocker_json("dup.py:1", "same defect A"),
        _blocker_json("dup.py:1", "same defect B"),
        _approve_json(),
    )
    result = await review_panel(goal="g", kind="implement_feature", diff=_DIFF, claude_caller=caller, n=3)
    assert result["verdict"] == "request_changes"
    assert len(result["blocking"]) == 1  # deduped by (location, severity)


# ============================ FAIL CLOSED ============================

async def test_sub_quorum_crash_raises_never_approves():
    """THE fail-closed property: with N=3, two panelists CRASH (non-votes) — below
    the ceil(3/2)=2 quorum — so the panel RAISES (fail closed + fast), it does NOT
    return approve. A crash must never become an approval. Even the one valid vote
    being 'approve' cannot rescue it: quorum is on VALID votes, and 1 < 2."""
    calls = {"i": 0}

    async def two_crash_one_approve(prompt: str) -> str:
        calls["i"] += 1
        if calls["i"] <= 2:
            raise RuntimeError("reviewer exploded")
        return _approve_json("looks fine to me")

    with pytest.raises(PlannerError) as ei:
        await review_panel(goal="g", kind="implement_feature", diff=_DIFF, claude_caller=two_crash_one_approve, n=3)
    msg = str(ei.value)
    assert "could not reach quorum" in msg
    assert "reviewer exploded" in msg  # actionable: names the failure
    assert "review it by hand" in msg


async def test_full_panel_crash_carries_quota_prose_for_pause_classification():
    """When ALL panelists hit a usage/session limit, the sub-quorum raise must
    carry the model's raw prose in PlannerError.raw so the queue's quota guard
    classifies it as a limit and PAUSES (not fails) — the #245 property, at the
    panel level."""
    async def session_limit(prompt: str) -> str:
        # a session limit comes back as prose, not JSON → extract_json raises
        # PlannerError with the prose in .raw inside the panelist.
        return "You've hit your session limit · resets 5:20pm"

    with pytest.raises(PlannerError) as ei:
        await review_panel(goal="g", kind="implement_feature", diff=_DIFF, claude_caller=session_limit, n=3)
    raw = ei.value.raw or ""
    assert "session limit" in raw  # quota prose survives for the classifier


async def test_quorum_met_ignores_a_single_non_vote():
    """A single non-vote below the quorum threshold does NOT sink the panel: with
    N=3, one crash + two valid votes (>= quorum 2) aggregates normally."""
    calls = {"i": 0}

    async def one_crash_two_approve(prompt: str) -> str:
        calls["i"] += 1
        if calls["i"] == 1:
            raise RuntimeError("transient blip")
        return _approve_json()

    result = await review_panel(goal="g", kind="implement_feature", diff=_DIFF, claude_caller=one_crash_two_approve, n=3)
    assert result["verdict"] == "approve"  # quorum reached on the 2 valid votes


# ============================ statefulness ===========================

async def test_each_panelist_vote_is_recorded_with_its_lens():
    """Stateful: every panelist's vote (lens, verdict, blocking_count, latency,
    error) is handed to the record_vote sink — the durable analog of an ephemeral
    fan-out. N votes, one per lens."""
    caller = _caller_returning(
        _approve_json(),
        _blocker_json("x.py", "bug"),
        _approve_json(),
    )
    votes: list[dict] = []
    await review_panel(
        goal="g", kind="implement_feature", diff=_DIFF, claude_caller=caller,
        n=3, record_vote=votes.append,
    )
    assert len(votes) == 3
    assert {v["lens"] for v in votes} == set(_REVIEW_LENSES)
    # the blocker panelist's vote carries its verdict + blocking count
    reg = next(v for v in votes if v["verdict"] == "request_changes")
    assert reg["blocking_count"] == 1
    assert all("latency_ms" in v for v in votes)


async def test_crashed_panelist_records_a_non_vote_with_error():
    """A crashing panelist still records a vote — verdict None, the error text
    captured — so the failure is legible in the durable trace, not silent."""
    calls = {"i": 0}

    async def one_crash(prompt: str) -> str:
        calls["i"] += 1
        if calls["i"] == 1:
            raise RuntimeError("kaboom")
        return _approve_json()

    votes: list[dict] = []
    await review_panel(
        goal="g", kind="implement_feature", diff=_DIFF, claude_caller=one_crash,
        n=3, record_vote=votes.append,
    )
    crashed = [v for v in votes if v["verdict"] is None]
    assert len(crashed) == 1
    assert "kaboom" in crashed[0]["error"]


async def test_empty_generated_diff_short_circuits_before_any_panelist():
    """The empty/generated-diff short-circuit runs ONCE, before spawning any
    panelist — a pure lockfile churn never fans out N model calls."""
    called = {"n": 0}

    async def caller(prompt: str) -> str:
        called["n"] += 1
        return _approve_json()

    gen_diff = (
        "diff --git a/package-lock.json b/package-lock.json\n"
        "--- a/package-lock.json\n+++ b/package-lock.json\n@@ -1 +1 @@\n-{}\n+{ }\n"
    )
    result = await review_panel(goal="g", kind="implement_feature", diff=gen_diff, claude_caller=caller, n=3)
    assert result["verdict"] == "approve"
    assert called["n"] == 0  # no panelist spawned


# ============================ queue integration ======================

@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _enable_gate_and_fake_diff(monkeypatch):
    monkeypatch.setattr(task_queue, "REVIEW_GATE_ENABLED", True)

    async def fake_diff(_host_dir, _base=""):
        return _DIFF

    monkeypatch.setattr(task_queue, "_git_diff", fake_diff)


def _ok_gate_runner(calls: list):
    async def runner(req: EngineRequest):
        calls.append(req.goal)
        gate = {"ran": True, "cmd": "pytest", "passed": True, "exit_code": 0,
                "timed_out": False, "output": ""}
        return {"status": "ok", "workspaceDir": req.workspace_dir, "verify": gate}
    return runner


def test_queue_default_reviewer_is_the_panel(store):
    """The queue's default reviewer is review_panel (a drop-in for review_diff) —
    so the opt-in path is wired but N=1 keeps today's behaviour."""
    q = TaskQueue(store)
    assert q._reviewer is review_panel


async def test_n1_through_queue_ships_like_the_single_reviewer(store, monkeypatch):
    """N=1 through the queue behaves like the single-reviewer gate: a clean
    approve ships the task done on the first try, no needless retry."""
    import functools
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)

    async def one_approve(prompt: str) -> str:
        return _approve_json()

    reviewer = functools.partial(review_panel, claude_caller=one_approve, n=1)
    calls: list = []
    q = TaskQueue(store, runner=_ok_gate_runner(calls), reviewer=reviewer)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert len(calls) == 1


async def test_panel_votes_persist_as_review_vote_events(store, monkeypatch):
    """Statefulness through the queue: running the panel at N=3 records three
    append-only ``review_vote`` events tied to the task — the durable analog of
    an ephemeral fan-out, a projection over which is the aggregate verdict."""
    import functools

    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)

    caller = _caller_returning(_approve_json(), _approve_json(), _approve_json())
    # Wire the REAL panel through the queue with a stub caller + N=3. functools
    # .partial preserves the record_vote kwarg so the queue passes its recorder.
    reviewer = functools.partial(review_panel, claude_caller=caller, n=3)

    calls: list = []
    q = TaskQueue(store, runner=_ok_gate_runner(calls), reviewer=reviewer)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()

    assert store.get_task(tid).status == "done"
    events = store.list_events(task_id=tid)
    votes = [e for e in events if e.type == "review_vote"]
    assert len(votes) == 3  # one per panelist/lens

    import json as _json
    lenses = {_json.loads(e.payload_json)["lens"] for e in votes}
    assert lenses == set(_REVIEW_LENSES)
