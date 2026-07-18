"""Regression tests for the console-facing Resume + Answer goal routes
(``POST /goals/{id}/resume`` and ``/answer`` in devclaw/server/http.py).

These give the operator two controls the console previously lacked — resume a
blocked goal whose blocker cleared out-of-band, and answer a firming question —
so the daily "it pinged me, I clear it" loop no longer needs an MCP call. The
routes are thin wrappers over goal_service.resume_goal / answer_unknowns; these
pin the wiring: the service is actually called with the right args, and each
failure mode maps to the right HTTP status (404 unknown goal, 400 bad input),
never a silent 200.
"""

from __future__ import annotations

import asyncio
import json

from starlette.requests import Request


def _req(path_params, body=None):
    scope = {
        "type": "http",
        "method": "POST",
        "path_params": path_params,
        "headers": [],
    }

    async def receive():
        raw = json.dumps(body).encode() if body is not None else b""
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive)


class _FakeGoals:
    """Minimal GoalService stand-in — records calls, raises injected errors."""

    def __init__(self, resume=None, answer=None):
        self._resume = resume
        self._answer = answer
        self.resume_calls: list = []
        self.answer_calls: list = []

    def resume_goal(self, goal_id):
        self.resume_calls.append(goal_id)
        if isinstance(self._resume, Exception):
            raise self._resume
        return self._resume

    async def answer_unknowns(self, goal_id, answers):
        self.answer_calls.append((goal_id, answers))
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


def _call(fn, req):
    resp = asyncio.run(fn(req))
    return resp.status_code, json.loads(resp.body)


# ── resume ─────────────────────────────────────────────────────────────────

def test_resume_calls_service_and_returns_result(monkeypatch):
    from devclaw.server import http as http_mod
    fake = _FakeGoals(resume={"goal_id": "g", "resumed": True})
    monkeypatch.setattr(http_mod, "goals", fake)
    status, body = _call(http_mod.goal_resume, _req({"goal_id": "g"}))
    assert status == 200 and body["resumed"] is True
    assert fake.resume_calls == ["g"]


def test_resume_unknown_goal_is_404(monkeypatch):
    from devclaw.server import http as http_mod
    monkeypatch.setattr(http_mod, "goals", _FakeGoals(resume=KeyError("g")))
    status, body = _call(http_mod.goal_resume, _req({"goal_id": "g"}))
    assert status == 404 and body["error"] == "not_found"


def test_resume_refused_in_firming_is_400(monkeypatch):
    # resume_goal raises ValueError for a goal blocked in firming (answers must
    # come through /answer) — must surface as 400 with the reason, not a 500.
    from devclaw.server import http as http_mod
    monkeypatch.setattr(http_mod, "goals", _FakeGoals(resume=ValueError("blocked in firming")))
    status, body = _call(http_mod.goal_resume, _req({"goal_id": "g"}))
    assert status == 400 and body["error"] == "cannot_resume" and "firming" in body["detail"]


# ── answer ─────────────────────────────────────────────────────────────────

def test_answer_forwards_answers_to_service(monkeypatch):
    from devclaw.server import http as http_mod
    fake = _FakeGoals(answer={"status": "firmed"})
    monkeypatch.setattr(http_mod, "goals", fake)
    status, _ = _call(http_mod.goal_answer, _req({"goal_id": "g"}, {"answers": {"u1": "yes"}}))
    assert status == 200
    assert fake.answer_calls == [("g", {"u1": "yes"})]


def test_answer_empty_map_is_400_without_calling_service(monkeypatch):
    from devclaw.server import http as http_mod
    fake = _FakeGoals(answer={"status": "firmed"})
    monkeypatch.setattr(http_mod, "goals", fake)
    status, body = _call(http_mod.goal_answer, _req({"goal_id": "g"}, {"answers": {}}))
    assert status == 400 and body["error"] == "answers_required"
    assert fake.answer_calls == []  # rejected before the service is touched


def test_answer_partial_or_extra_is_400(monkeypatch):
    # answer_unknowns raises ValueError when the map doesn't cover every unknown.
    from devclaw.server import http as http_mod
    monkeypatch.setattr(http_mod, "goals", _FakeGoals(answer=ValueError("missing=['u2']")))
    status, body = _call(http_mod.goal_answer, _req({"goal_id": "g"}, {"answers": {"u1": "yes"}}))
    assert status == 400 and body["error"] == "bad_answers" and "u2" in body["detail"]


def test_answer_unknown_goal_is_404(monkeypatch):
    from devclaw.server import http as http_mod
    monkeypatch.setattr(http_mod, "goals", _FakeGoals(answer=KeyError("g")))
    status, body = _call(http_mod.goal_answer, _req({"goal_id": "g"}, {"answers": {"u1": "y"}}))
    assert status == 404 and body["error"] == "not_found"
