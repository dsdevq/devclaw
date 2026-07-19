"""`devclaw cognition decompose` — the no-docker decomposer dry-run.

Fully stubbed: a fake claude caller returns a canned checklist, so these
exercise the real CLI wiring (prompt build → ONE cognition call → parse →
render) with NO docker, NO task queue, NO state store, NO real claude.
(`cognition plan` retired with plan_goal — ADR 0003 stage 1: programs plan
through the decomposer, and this dry-run inspects that ONE spine.) The named
behaviors:

- `decompose` renders the checklist (milestones, dependency edges,
  evidence targets, open questions) from a canned decomposer response;
- `--json` emits the parsed checklist;
- the command makes exactly ONE cognition call and constructs no
  registry/GoalStore/StateStore (the queue/engine are never reachable).
"""
from __future__ import annotations

import json

import pytest

import devclaw.cli as cli
from devclaw.loom import trace as _trace


# ---- canned model response -------------------------------------------------

_CHECKLIST = """checklist:
  - id: item1
    requirement: Wire GetAccounts.Execute to the query handler
    evidence_target: src/Accounts/GetAccounts.cs calls IQueryHandler
    milestone: accounts
    depends_on: []
  - id: item2
    requirement: Add an integration test for GET /accounts
    evidence_target: tests/AccountsTests.cs asserts a 200 with a real body
    milestone: accounts
    depends_on: [item1]
open_questions:
  - which auth scheme should protect the endpoint?
notes:
  - GetAccounts currently ships as a not_yet_available stub
"""


class _FakeCaller:
    """A one-arg async caller that records ONE cognition event (so the CLI's
    latency/token surface is exercised) and counts its own invocations."""

    def __init__(self, response: str, *, role: str) -> None:
        self.response = response
        self.role = role
        self.calls: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        _trace.record_cognition(
            role=self.role, model="opus", prompt=prompt, response=self.response,
            latency_ms=1234, tokens_in=1500, tokens_out=420, cost_usd=0.0188,
        )
        return self.response


@pytest.fixture
def fake_decomposer(monkeypatch):
    caller = _FakeCaller(_CHECKLIST, role="goal_decomposer")
    monkeypatch.setattr(cli, "_default_decomposer_caller", lambda: caller)
    return caller


# ---- decompose render ------------------------------------------------------


def test_decompose_renders_checklist(fake_decomposer, capsys):
    rc = cli.main([
        "cognition", "decompose", "Ship the accounts API",
        "--done-when", "GET /accounts returns real data",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "milestone: accounts" in out
    assert "Wire GetAccounts.Execute to the query handler" in out
    assert "evidence_target:" in out
    assert "← depends_on: item1" in out
    assert "which auth scheme should protect the endpoint?" in out
    assert "cognition_calls=1" in out
    assert len(fake_decomposer.calls) == 1


def test_decompose_json_emits_parsed_checklist(fake_decomposer, capsys):
    rc = cli.main([
        "cognition", "decompose", "Ship it",
        "--done-when", "done", "--json",
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert [i["id"] for i in data["checklist"]] == ["item1", "item2"]
    assert data["checklist"][1]["depends_on"] == ["item1"]
    assert data["open_questions"] == ["which auth scheme should protect the endpoint?"]


# ---- side-effect contract --------------------------------------------------


def test_decompose_makes_exactly_one_cognition_call(fake_decomposer, capsys):
    assert cli.main([
        "cognition", "decompose", "Do the thing", "--done-when", "done",
    ]) == 0
    assert len(fake_decomposer.calls) == 1
    assert "cognition_calls=1" in capsys.readouterr().out


def test_decompose_touches_no_registry_goalstore_or_statestore(fake_decomposer, monkeypatch, capsys):
    """The dry-run is inspection-only: it must construct NO ProjectRegistry,
    NO GoalStore, NO StateStore — the queue/engine are unreachable from here.
    Booby-trap each constructor; the command must still succeed."""
    def _boom(*_a, **_k):  # pragma: no cover - only fires on a violation
        raise AssertionError("cognition dry-run must not open any store")

    monkeypatch.setattr(cli, "ProjectRegistry", _boom)
    monkeypatch.setattr(cli, "GoalStore", _boom)
    monkeypatch.setattr(cli, "StateStore", _boom)
    assert cli.main([
        "cognition", "decompose", "Isolated planning", "--done-when", "done",
    ]) == 0
    assert len(fake_decomposer.calls) == 1


def test_retired_plan_subcommand_is_gone():
    """`cognition plan` retired with plan_goal (ADR 0003 stage 1) — argparse
    must reject it rather than silently doing something else."""
    with pytest.raises(SystemExit):
        cli.main(["cognition", "plan", "anything"])
