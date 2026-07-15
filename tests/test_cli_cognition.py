"""`devclaw cognition plan|decompose` — the no-docker planner/decomposer dry-run.

Fully stubbed: a fake claude caller returns canned DAG / checklist output, so
these exercise the real CLI wiring (prompt build → ONE cognition call → parse →
render) with NO docker, NO task queue, NO state store, NO real claude. The
named behaviors:

- `plan` renders the DAG with kinds, dependency edges, and the #252
  acceptance-criteria / constraints sections carried in each task's goal brief;
- `--repo` injects a REPOSITORY CONTEXT block into the prompt; absent without it
  (asserted against a marker proven absent from the raw plan-goal template);
- `--json` emits the parsed plan; `-v` includes the exact prompt;
- the command makes exactly ONE cognition call and constructs no
  registry/GoalStore/StateStore (the queue/engine are never reachable);
- `decompose` renders the checklist from a canned decomposer response.
"""
from __future__ import annotations

import json

import pytest

import devclaw.cli as cli
from devclaw.loom import trace as _trace


# ---- canned model responses ------------------------------------------------

_PLAN = {
    "tasks": [
        {
            "key": "scaffold",
            "goal": (
                "Create the .NET minimal API skeleton.\n"
                "Acceptance criteria:\n"
                "- the solution builds\n"
                "- the health endpoint returns 200"
            ),
            "kind": "implement_feature",
            "depends_on": [],
        },
        {
            "key": "api",
            "goal": (
                "Implement the accounts endpoints.\n"
                "Acceptance criteria:\n"
                "- GET /accounts returns the list\n"
                "Constraints:\n"
                "- do not touch the auth module"
            ),
            "kind": "implement_feature",
            "depends_on": ["scaffold"],
        },
        {
            "key": "audit",
            "goal": (
                "Review the repo for security issues.\n"
                "Acceptance criteria:\n"
                "- no hardcoded secrets remain"
            ),
            "kind": "review_repository",
            "depends_on": ["scaffold"],
        },
        {
            "key": "ui",
            "goal": (
                "Build the Angular accounts screen.\n"
                "Acceptance criteria:\n"
                "- the screen lists accounts"
            ),
            "kind": "implement_feature",
            "depends_on": ["api"],
        },
    ]
}
_PLAN_JSON = json.dumps(_PLAN)

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
def fake_planner(monkeypatch):
    caller = _FakeCaller(_PLAN_JSON, role="planner")
    monkeypatch.setattr(cli, "_default_planner_caller", lambda: caller)
    return caller


@pytest.fixture
def fake_decomposer(monkeypatch):
    caller = _FakeCaller(_CHECKLIST, role="goal_decomposer")
    monkeypatch.setattr(cli, "_default_decomposer_caller", lambda: caller)
    return caller


# ---- plan render -----------------------------------------------------------


def test_plan_renders_dag_with_kinds_deps_and_acceptance_criteria(fake_planner, capsys):
    assert cli.main(["cognition", "plan", "Build a fullstack accounts app"]) == 0
    out = capsys.readouterr().out
    # kinds surfaced
    assert "[implement_feature]" in out
    assert "[review_repository]" in out
    # dependency edges rendered as arrows
    assert "← depends_on: scaffold" in out
    assert "← depends_on: api" in out
    # #252 acceptance-criteria + constraints sections carried in the brief
    assert "Acceptance criteria:" in out
    assert "Constraints:" in out
    assert "do not touch the auth module" in out
    # parallel-vs-sequential composition is legible (api + audit both on scaffold)
    assert "no dependencies" in out
    assert "parallel" in out


# ---- repo-context grounding ------------------------------------------------


def test_repo_flag_injects_repository_context(fake_planner, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "_review_repo_context_sync",
        lambda _dir: "remote: git@github.com:me/accounts.git\nbranch: main\nProgram.cs (present)",
    )
    # -v prints the exact prompt, so the injected block is observable in stdout.
    assert cli.main(["cognition", "plan", "Ship it", "--repo", "/repos/accounts", "-v"]) == 0
    out = capsys.readouterr().out
    # the marker below is injected by build_planner_prompt, NOT present in the
    # raw plan-goal template (asserted separately) — so its presence proves the
    # REPOSITORY CONTEXT block was rendered.
    assert "facts from the task workspace" in out
    assert "git@github.com:me/accounts.git" in out


def test_no_repo_omits_repository_context_block(fake_planner, capsys):
    assert cli.main(["cognition", "plan", "Ship it", "-v"]) == 0
    out = capsys.readouterr().out
    # Absence marker: the injected-block phrasing is proven absent from the raw
    # plan-goal template (see test below), so its absence here is meaningful and
    # not vacuous (per .claude/rules/testing.md).
    assert "facts from the task workspace" not in out


def test_absence_marker_is_absent_from_raw_template():
    """Guards the two tests above: the injected-block marker must NOT appear in
    the raw plan-goal template, or the omission assertion would be vacuous."""
    from devclaw.prompts import load_prompt

    assert "facts from the task workspace" not in load_prompt("plan-goal")


# ---- --json / -v -----------------------------------------------------------


def test_json_emits_parsed_plan(fake_planner, capsys):
    assert cli.main(["cognition", "plan", "Build it", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [t["key"] for t in data] == ["scaffold", "api", "audit", "ui"]
    assert data[1]["depends_on"] == ["scaffold"]
    assert data[2]["kind"] == "review_repository"
    # the acceptance-criteria/constraints text rides inside the task goal
    assert "Constraints:" in data[1]["goal"]


def test_show_prompt_includes_the_exact_prompt(fake_planner, capsys):
    assert cli.main(["cognition", "plan", "Add a /health endpoint", "-v"]) == 0
    out = capsys.readouterr().out
    assert "=== PROMPT ===" in out
    # the goal text and the planner system prompt both land in the sent prompt
    assert "Add a /health endpoint" in out
    assert "DevClaw's planner" in out


# ---- side-effect contract --------------------------------------------------


def test_makes_exactly_one_cognition_call(fake_planner, capsys):
    assert cli.main(["cognition", "plan", "Do the thing"]) == 0
    assert len(fake_planner.calls) == 1
    # the render also reports the count it observed via its in-memory tracer
    assert "cognition_calls=1" in capsys.readouterr().out


def test_touches_no_registry_goalstore_or_statestore(fake_planner, monkeypatch, capsys):
    """The dry-run is inspection-only: it must construct NO ProjectRegistry,
    NO GoalStore, NO StateStore — the queue/engine are unreachable from here.
    Booby-trap each constructor; the command must still succeed."""
    def _boom(*_a, **_k):  # pragma: no cover - only fires on a violation
        raise AssertionError("cognition dry-run must not open any store")

    monkeypatch.setattr(cli, "ProjectRegistry", _boom)
    monkeypatch.setattr(cli, "GoalStore", _boom)
    monkeypatch.setattr(cli, "StateStore", _boom)
    assert cli.main(["cognition", "plan", "Isolated planning"]) == 0
    assert len(fake_planner.calls) == 1


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
