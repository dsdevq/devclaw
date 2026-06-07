"""The /goals/answer route — deterministic reply→goal routing for the devclaw
Telegram bridge. Routes to the single grilling/plan_review goal; 409 on
none/ambiguous; 400 on empty."""

from __future__ import annotations

import pytest

import devclaw.server as server


class _Req:
    def __init__(self, body, bad=False):
        self._body, self._bad = body, bad

    async def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._body


class _Goals:
    def __init__(self, goals):
        self._goals, self.answered = goals, []

    def list_goals(self):
        return self._goals

    def answer_goal(self, gid, text):
        self.answered.append((gid, text))
        return {"goal_id": gid, "routed_to": "grill", "recorded": True}


@pytest.mark.asyncio
async def test_routes_to_single_waiting_goal(monkeypatch):
    fg = _Goals([{"id": "g1", "lifecycle": "grilling"}, {"id": "g2", "lifecycle": "executing"}])
    monkeypatch.setattr(server, "goals", fg)
    resp = await server.goals_answer(_Req({"text": "Postgres"}))
    assert resp.status_code == 200
    assert fg.answered == [("g1", "Postgres")]


@pytest.mark.asyncio
async def test_plan_review_also_routes(monkeypatch):
    fg = _Goals([{"id": "g1", "lifecycle": "plan_review"}])
    monkeypatch.setattr(server, "goals", fg)
    resp = await server.goals_answer(_Req({"text": "approved"}))
    assert resp.status_code == 200 and fg.answered == [("g1", "approved")]


@pytest.mark.asyncio
async def test_409_when_none_waiting(monkeypatch):
    fg = _Goals([{"id": "g1", "lifecycle": "executing"}])
    monkeypatch.setattr(server, "goals", fg)
    resp = await server.goals_answer(_Req({"text": "hi"}))
    assert resp.status_code == 409 and fg.answered == []


@pytest.mark.asyncio
async def test_409_when_multiple_waiting(monkeypatch):
    fg = _Goals([{"id": "g1", "lifecycle": "grilling"}, {"id": "g2", "lifecycle": "plan_review"}])
    monkeypatch.setattr(server, "goals", fg)
    resp = await server.goals_answer(_Req({"text": "hi"}))
    assert resp.status_code == 409 and fg.answered == []


@pytest.mark.asyncio
async def test_400_missing_text(monkeypatch):
    fg = _Goals([{"id": "g1", "lifecycle": "grilling"}])
    monkeypatch.setattr(server, "goals", fg)
    resp = await server.goals_answer(_Req({"text": "   "}))
    assert resp.status_code == 400 and fg.answered == []
