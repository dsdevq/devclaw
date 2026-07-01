"""Dry cognition MCP tools — pure-cognition wrappers that never file a goal.

These tools exist so the customer (via the waiter, or a Claude Code session with
devclaw-mcp registered) can *think about* a project — see what world-research
would say, see how the decomposer would break it up, see how the evaluator would
grade a hypothetical review — without workspace_dir / repo_url / a persisted
goal. The pins here guard three properties:

  1. Each tool returns the module's actual artifact shape (brief markdown / YAML
     checklist / EvalResult JSON) — so a caller can chain scope_grill →
     dry_world_research → dry_decompose → dry_evaluate and get real cognition
     back at every stage.
  2. Each tool refuses empty inputs with a ToolError — no silent stub runs.
  3. NO goal is created and NO file lands under the goals dir — dry means dry.
"""

from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError

from devclaw.server import tools as _tools


# --------------------------- helpers ---------------------------


def _stub_caller(response: str):
    """Build an async claude_caller that returns a canned response."""

    async def _call(_prompt: str) -> str:
        return response

    return _call


@pytest.fixture(autouse=True)
def _no_goals_dir_writes(tmp_path, monkeypatch):
    """Belt-and-braces: even if a dry tool tried to write a goal, redirect the
    store's dir to tmp_path so nothing lands in the real goals dir. Then assert
    the dir stays empty at the end of each test."""
    from devclaw.server import _state

    goals_dir = tmp_path / "goals"
    monkeypatch.setattr(_state.goals._store, "goals_dir", goals_dir, raising=False)
    yield
    assert not goals_dir.exists() or not any(goals_dir.iterdir()), (
        f"dry cognition tool wrote to goals_dir at {goals_dir}"
    )


# --------------------------- dry_world_research ---------------------------


async def test_dry_world_research_returns_brief(monkeypatch):
    from devclaw.goal import world_research as _world

    brief = (
        "## Real-world exemplars\n- HubSpot: contact-first, deal pipeline\n"
        "## What good MVP looks like\n- Auth, contacts, one pipeline view\n"
        "## Deliberately defer\n- Reporting, integrations"
    )
    monkeypatch.setattr(_world, "default_caller", lambda: _stub_caller(brief))

    result = await _tools.dry_world_research(
        objective="build a CRM for SMB sales teams",
    )
    assert result == brief
    assert "## Real-world exemplars" in result


async def test_dry_world_research_rejects_empty_objective():
    with pytest.raises(ToolError, match="non-empty objective"):
        await _tools.dry_world_research(objective="   ")


async def test_dry_world_research_surfaces_module_errors(monkeypatch):
    from devclaw.goal import world_research as _world

    monkeypatch.setattr(_world, "default_caller", lambda: _stub_caller("   "))
    with pytest.raises(ToolError, match="dry_world_research failed"):
        await _tools.dry_world_research(objective="build a CRM")


# --------------------------- dry_decompose ---------------------------


_MIN_CHECKLIST_YAML = """
checklist:
  - id: t1
    milestone: m1
    requirement: add a GET /health endpoint returning 200
    addresses_files:
      - src/routes/health.py
    evidence_target: src/routes/health.py contains a router with GET /health
    depends_on: []
    model_tier: sonnet
open_questions: []
""".strip()


async def test_dry_decompose_returns_checklist_yaml(monkeypatch):
    from devclaw.goal import decomposer as _decomp

    monkeypatch.setattr(_decomp, "default_caller", lambda: _stub_caller(_MIN_CHECKLIST_YAML))

    yaml_out = await _tools.dry_decompose(
        objective="add a health endpoint to the API",
        done_when="GET /health returns 200",
        backlog=["add health route"],
    )
    assert "checklist:" in yaml_out
    assert "add a GET /health endpoint" in yaml_out
    assert "src/routes/health.py" in yaml_out


async def test_dry_decompose_rejects_empty_objective():
    with pytest.raises(ToolError, match="non-empty objective"):
        await _tools.dry_decompose(objective="")


async def test_dry_decompose_surfaces_schema_errors(monkeypatch):
    from devclaw.goal import decomposer as _decomp

    monkeypatch.setattr(_decomp, "default_caller", lambda: _stub_caller("not valid yaml: ][["))
    with pytest.raises(ToolError, match="dry_decompose failed"):
        await _tools.dry_decompose(objective="add a health endpoint")


# --------------------------- dry_evaluate ---------------------------


_ACHIEVED_JSON = json.dumps(
    {
        "verdict": "achieved",
        "rationale": "all clauses satisfied by file:line evidence",
        "clauses": [
            {
                "clause": "GET /health returns 200",
                "satisfied": True,
                "evidence": "src/routes/health.py:12 (asserted by tests/test_health.py:8)",
            }
        ],
    }
)

_OFF_TRACK_JSON = json.dumps(
    {
        "verdict": "off_track",
        "rationale": "the shipped code is a stub disguised as a build",
        "corrections": ["replace the disabled button with a working handler"],
        "clauses": [
            {
                "clause": "Edit button opens a modal",
                "satisfied": False,
                "evidence": "AccountDetailView.tsx:12 — <button disabled>Edit</button> — no onClick",
            }
        ],
    }
)


async def test_dry_evaluate_returns_achieved_verdict(monkeypatch):
    from devclaw.goal import evaluator as _eval

    monkeypatch.setattr(_eval, "default_caller", lambda: _stub_caller(_ACHIEVED_JSON))

    raw = await _tools.dry_evaluate(
        objective="ship the /health endpoint",
        done_when="GET /health returns 200",
        review_report=(
            "## Per-clause evidence\n"
            "1. GET /health returns 200 — SATISFIED — src/routes/health.py:12"
        ),
    )
    result = json.loads(raw)
    assert result["verdict"] == "achieved"
    assert result["clauses"][0]["satisfied"] is True


async def test_dry_evaluate_returns_stub_disguise_off_track(monkeypatch):
    from devclaw.goal import evaluator as _eval

    monkeypatch.setattr(_eval, "default_caller", lambda: _stub_caller(_OFF_TRACK_JSON))

    raw = await _tools.dry_evaluate(
        objective="build the account edit modal",
        done_when="Edit button opens a modal that PATCHes /accounts/{id}",
        review_report="## Per-clause evidence\n1. Edit button opens modal — UNSATISFIED",
    )
    result = json.loads(raw)
    assert result["verdict"] == "off_track"
    assert result["clauses"][0]["satisfied"] is False
    assert "replace the disabled button" in result["corrections"][0]


async def test_dry_evaluate_rejects_missing_done_when():
    with pytest.raises(ToolError, match="done_when"):
        await _tools.dry_evaluate(
            objective="build X", done_when="   ", review_report="anything"
        )


async def test_dry_evaluate_rejects_missing_objective():
    with pytest.raises(ToolError, match="non-empty objective"):
        await _tools.dry_evaluate(
            objective="", done_when="ship it", review_report="anything"
        )


# --------------------------- purity guard ---------------------------


async def test_dry_tools_do_not_use_goal_store(monkeypatch, tmp_path):
    """The negative pin: none of the three tools should call goals.create_goal
    or verify_goal or otherwise interact with the goal store. If any of them
    ever grows a side-effecting import path, this test flips red."""
    from devclaw.goal import decomposer as _decomp
    from devclaw.goal import evaluator as _eval
    from devclaw.goal import world_research as _world
    from devclaw.server import _state

    calls: list[str] = []
    original_create = _state.goals.create_goal
    original_verify = _state.goals.verify_goal

    def tripwire_create(*a, **k):
        calls.append("create_goal")
        return original_create(*a, **k)

    def tripwire_verify(*a, **k):
        calls.append("verify_goal")
        return original_verify(*a, **k)

    monkeypatch.setattr(_state.goals, "create_goal", tripwire_create)
    monkeypatch.setattr(_state.goals, "verify_goal", tripwire_verify)
    monkeypatch.setattr(_world, "default_caller", lambda: _stub_caller("## brief"))
    monkeypatch.setattr(_decomp, "default_caller", lambda: _stub_caller(_MIN_CHECKLIST_YAML))
    monkeypatch.setattr(_eval, "default_caller", lambda: _stub_caller(_ACHIEVED_JSON))

    await _tools.dry_world_research(objective="build a CRM")
    await _tools.dry_decompose(objective="add /health", done_when="200")
    await _tools.dry_evaluate(
        objective="ship /health", done_when="GET /health returns 200",
        review_report="## Per-clause evidence\n1. satisfied",
    )

    assert calls == [], f"dry tools reached the goal store: {calls}"
