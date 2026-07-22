"""Regression tests for the browser-gate reachability escape valve.

The browser-E2E gate fires MECHANICALLY on any frontend file. Its one legitimate
false positive is a UI change not rendered in the running app (a library component
no route imports yet — the cmn-tab-group wedge, 2026-07-17). This escape valve
lets an INDEPENDENT, grounded judge relax that block — but ONLY on an affirmative,
proven "not reachable"; every other outcome (reachable / unknown / a real browser
failure / a judge crash / disabled) leaves the fail-closed block standing.

Two halves, mirroring the review gate:
  1. the pure module (devclaw/quality/reachability.py): prompt build, verdict
     normalization, JSON parse.
  2. the queue integration (_browser_reachability_clears + settle): only ever
     downgrades a NO-RUN block, never a real failure; fails closed on any doubt;
     no cognition on idle/backend/passing paths (the zero-token guard).

Driven with a stubbed judge — no docker, no claude.
"""

from __future__ import annotations

import pytest

from devclaw import task_queue
from devclaw.planner import PlannerError
from devclaw.quality.reachability import (
    build_reachability_prompt,
    judge_reachability,
    validate_reachability,
)
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue

_FRONTEND_DIFF = (
    "diff --git a/frontend/src/app/select/select.component.ts "
    "b/frontend/src/app/select/select.component.ts\n"
    "--- a/frontend/src/app/select/select.component.ts\n"
    "+++ b/frontend/src/app/select/select.component.ts\n"
    "@@ -1 +1 @@\n-old\n+new\n"
)
_BACKEND_DIFF = (
    "diff --git a/backend/Api/ContactsController.cs b/backend/Api/ContactsController.cs\n"
    "--- a/backend/Api/ContactsController.cs\n+++ b/backend/Api/ContactsController.cs\n"
    "@@ -1 +1 @@\n-old\n+new\n"
)


def _verify(browser_report=None):
    v = {"ran": True, "passed": True, "cmd": "ng build && vitest run"}
    if browser_report is not None:
        v["browser_report"] = browser_report
    return v


def _with_pw_config(tmp_path):
    fe = tmp_path / "frontend"
    fe.mkdir()
    (fe / "playwright.config.ts").write_text("export default {};\n")
    return str(tmp_path)


def _judge(answer, *, calls=None, rationale="because"):
    """A stub reachability judge returning a fixed verdict, recording call count."""
    async def judge(*, diff, repo_context=None):
        if calls is not None:
            calls.append(diff)
        return {"reachable": answer, "rationale": rationale}
    return judge


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _no_git_context(monkeypatch):
    # The escape valve grounds the judge in a repo snapshot via _review_repo_context
    # (a git subprocess). Stub it to "" so these unit tests stay fast + hermetic —
    # the stub judge ignores context anyway, and "" is exactly what a non-repo
    # workspace degrades to in production (→ judge answers unknown → block stands).
    async def _empty(_ws):
        return ""
    monkeypatch.setattr(task_queue, "_review_repo_context", _empty)


# ============================ pure module ============================

def test_build_prompt_includes_contract_context_and_diff():
    p = build_reachability_prompt(
        diff="diff --git a/x b/x", repo_context="routes: /home -> HomeComponent"
    )
    assert "reachable" in p and "Playwright" in p
    # assert the INJECTED context block (distinctive marker), not a prose mention —
    # the contract text must not leak the header or the omission test goes vacuous.
    assert "REPOSITORY CONTEXT (facts" in p and "HomeComponent" in p
    assert "diff --git a/x" in p


def test_build_prompt_omits_context_section_when_blank():
    p = build_reachability_prompt(diff="d", repo_context="   ")
    assert "REPOSITORY CONTEXT (facts" not in p


def test_prompt_contract_fails_closed_on_doubt():
    """The contract must bias hard toward requiring the run: unknown is the safe
    answer, and 'not reachable' carries the burden of proof."""
    p = build_reachability_prompt(diff="d")
    assert '`unknown`' in p or "unknown" in p
    assert "fail closed" in p.lower()


def test_validate_out_of_range_reachable_coerces_to_unknown():
    # Anything not in yes/no/unknown → unknown (the SAFE default, never downgrades).
    assert validate_reachability({"reachable": "maybe"})["reachable"] == "unknown"
    assert validate_reachability({"reachable": True})["reachable"] == "unknown"
    assert validate_reachability({})["reachable"] == "unknown"
    assert validate_reachability("nope")["reachable"] == "unknown"


def test_validate_passes_valid_verdicts():
    for v in ("yes", "no", "unknown"):
        assert validate_reachability({"reachable": v, "rationale": "r"})["reachable"] == v


async def test_judge_raises_on_unparseable_response():
    async def caller(_prompt):
        return "You've hit your usage limit · resets 5pm"  # no JSON
    with pytest.raises(PlannerError):
        await judge_reachability(diff="d", claude_caller=caller)


async def test_judge_returns_normalized_verdict():
    async def caller(_prompt):
        return '{"reachable": "no", "rationale": "cmn-tab-group is imported by no route"}'
    out = await judge_reachability(diff="d", claude_caller=caller)
    assert out["reachable"] == "no" and "no route" in out["rationale"]


# ===================== escape-valve unit (fail-closed spine) =====================

async def test_clears_only_on_proven_not_reachable(store, tmp_path):
    ws = _with_pw_config(tmp_path)
    calls: list = []
    q = TaskQueue(store, reachability_judge=_judge("no", calls=calls))
    # never_ran (frontend diff, config present, no browser_report) + reachable "no"
    assert await q._browser_reachability_clears(_verify(), _FRONTEND_DIFF, ws) is True
    assert len(calls) == 1  # the judge WAS consulted on the would-block path


async def test_does_not_clear_when_reachable_yes(store, tmp_path):
    ws = _with_pw_config(tmp_path)
    q = TaskQueue(store, reachability_judge=_judge("yes"))
    assert await q._browser_reachability_clears(_verify(), _FRONTEND_DIFF, ws) is False


async def test_does_not_clear_on_unknown(store, tmp_path):
    ws = _with_pw_config(tmp_path)
    q = TaskQueue(store, reachability_judge=_judge("unknown"))
    assert await q._browser_reachability_clears(_verify(), _FRONTEND_DIFF, ws) is False


async def test_judge_crash_leaves_block_standing(store, tmp_path):
    ws = _with_pw_config(tmp_path)

    async def boom(*, diff, repo_context=None):
        raise RuntimeError("judge exploded")

    q = TaskQueue(store, reachability_judge=boom)
    # fail closed — a crash is never an override
    assert await q._browser_reachability_clears(_verify(), _FRONTEND_DIFF, ws) is False


async def test_real_browser_failure_is_never_reasoned_away(store, tmp_path):
    """ran_failed (a browser suite executed and a test FAILED) is hard evidence,
    not a false positive — the judge must NOT even be consulted, and the block
    must stand no matter what a judge would say."""
    ws = _with_pw_config(tmp_path)
    calls: list = []
    q = TaskQueue(store, reachability_judge=_judge("no", calls=calls))
    failed = _verify({"expected": 5, "unexpected": 1, "flaky": 0, "skipped": 0})
    assert await q._browser_reachability_clears(failed, _FRONTEND_DIFF, ws) is False
    assert calls == []  # zero-token: a real failure is never reasoned away


async def test_passing_browser_run_never_consults_judge(store, tmp_path):
    """A ran_passed verdict isn't a block at all → the judge is never called
    (zero-token guard)."""
    ws = _with_pw_config(tmp_path)
    calls: list = []
    q = TaskQueue(store, reachability_judge=_judge("no", calls=calls))
    passed = _verify({"expected": 3, "unexpected": 0, "flaky": 0, "skipped": 0})
    assert await q._browser_reachability_clears(passed, _FRONTEND_DIFF, ws) is False
    assert calls == []


async def test_backend_change_never_consults_judge(store, tmp_path):
    """A backend-only diff never triggers the gate → not a candidate → no
    cognition (zero-token guard)."""
    ws = _with_pw_config(tmp_path)
    calls: list = []
    q = TaskQueue(store, reachability_judge=_judge("no", calls=calls))
    assert await q._browser_reachability_clears(_verify(), _BACKEND_DIFF, ws) is False
    assert calls == []


async def test_disabled_flag_is_a_noop(store, tmp_path, monkeypatch):
    monkeypatch.setattr(task_queue, "BROWSER_REACHABILITY_ENABLED", False)
    ws = _with_pw_config(tmp_path)
    calls: list = []
    q = TaskQueue(store, reachability_judge=_judge("no", calls=calls))
    assert await q._browser_reachability_clears(_verify(), _FRONTEND_DIFF, ws) is False
    assert calls == []  # disabled → judge never consulted


async def test_absent_suite_no_run_is_an_override_candidate(store, tmp_path):
    """A frontend change in a project with NO playwright config (absent) that has
    reached the block path (strict mode) is also a no-run false-positive candidate
    — a proven-unreachable verdict clears it too."""
    (tmp_path / "frontend").mkdir()  # no playwright config → verdict 'absent'
    q = TaskQueue(store, reachability_judge=_judge("no"))
    assert await q._browser_reachability_clears(_verify(), _FRONTEND_DIFF, str(tmp_path)) is True


# ========================= settle integration =========================

def _ok_frontend_runner():
    """Agent ok + verify gate passes, but NO browser_report — so the browser gate
    is the only thing that can send the task back."""
    async def runner(req):
        gate = {"ran": True, "passed": True, "cmd": "ng build", "exit_code": 0,
                "timed_out": False, "output": ""}
        return {"status": "ok", "workspaceDir": req.workspace_dir, "verify": gate}
    return runner


async def _approve(*, goal, kind, diff, repo_context=None):
    return {"verdict": "approve", "summary": "ok", "issues": [], "blocking": []}


@pytest.fixture()
def _frontend_settle(monkeypatch, tmp_path):
    # A real workspace with a playwright config (→ config_present → never_ran),
    # a frontend diff, gate + review forced to pass so only the browser gate bites.
    monkeypatch.setattr(task_queue, "REVIEW_GATE_ENABLED", True)
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    ws = _with_pw_config(tmp_path)

    async def fake_diff(_host, _base=""):
        return _FRONTEND_DIFF
    monkeypatch.setattr(task_queue, "_git_diff", fake_diff)
    return ws


async def test_settle_ships_when_reachability_clears(store, _frontend_settle):
    """End-to-end: a UI change with no browser run that the judge proves is not
    rendered in the app SHIPS (done) — the escape valve cleared the block."""
    ws = _frontend_settle
    q = TaskQueue(
        store, runner=_ok_frontend_runner(), reviewer=_approve,
        reachability_judge=_judge("no"),
    )
    tid = q.submit(kind="implement_feature", workspace_dir=ws, goal="add cmn-tab-group", verify_cmd="ng build")
    await q.drain()
    assert store.get_task(tid).status == "done"


async def test_settle_blocks_when_reachability_says_reachable(store, _frontend_settle):
    """The converse: a UI change the judge says IS reachable still fails closed on
    the browser gate — the escape valve does not fire."""
    ws = _frontend_settle
    q = TaskQueue(
        store, runner=_ok_frontend_runner(), reviewer=_approve,
        reachability_judge=_judge("yes"),
    )
    # strictness="strict": this asserts the browser gate FAILS CLOSED. The
    # default is now "trust" (advisory, ADR 0007), under which a reachable-but-
    # unrun UI change ships-with-advisory instead — covered separately.
    tid = q.submit(kind="implement_feature", workspace_dir=ws, goal="wire tab-group into settings page", verify_cmd="ng build", strictness="strict")
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "failed"
    assert task_queue._BROWSER_GATE_MARKER in (t.error or "")
