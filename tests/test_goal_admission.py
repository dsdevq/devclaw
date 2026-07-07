"""Chef-side goal admission — the rejection path.

The warning path (bare verify_cmd → admit + flag) lives in
``test_create_goal_warnings.py``. Here we exercise the REJECT path: each test
shapes a malformed goal and asserts both (a) ``verify_goal`` returns the right
machine-readable condition codes, and (b) ``create_goal`` raises
``GoalAdmissionRejected`` carrying the same conditions.

The "machine-readable codes" contract is load-bearing — the waiter (or the
chain test) routes on them. Asserting on codes (not message prose) keeps the
tests stable when we tighten wording later.
"""

from __future__ import annotations

import pytest

from devclaw.goal.admission import (
    AdmissionResult,
    GoalAdmissionRejected,
    verify_goal,
)
from devclaw.goal.service import GoalConfig, GoalService
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


@pytest.fixture()
def svc(tmp_path):
    store = StateStore(str(tmp_path / "t.db"))
    queue = TaskQueue(store)
    cfg = GoalConfig(
        goals_dir=tmp_path / "goals",
        notify_url="",
        tick_seconds=900,
        eval_every=5,
        verify_done=False,
    )
    svc = GoalService(queue, store, cfg)
    yield svc
    store.close()


def _codes(result: AdmissionResult) -> set[str]:
    return {c.code for c in result.conditions}


def _reject_codes(result: AdmissionResult) -> set[str]:
    return {c.code for c in result.rejections}


# ---- verify_goal (pure check) -----------------------------------------------


def test_empty_objective_is_rejected():
    r = verify_goal(
        objective="", workspace_dir="/ws", done_when="x" * 30, backlog=["item"],
    )
    assert not r.admitted
    assert "missing_objective" in _reject_codes(r)


def test_whitespace_objective_is_rejected():
    r = verify_goal(
        objective="   \n  ", workspace_dir="/ws",
        done_when="x" * 30, backlog=["item"],
    )
    assert "missing_objective" in _reject_codes(r)


def test_empty_workspace_dir_is_rejected():
    r = verify_goal(
        objective="ship X", workspace_dir="",
        done_when="x" * 30, backlog=["item"],
    )
    assert "missing_workspace_dir" in _reject_codes(r)


def test_no_done_when_and_no_spec_is_rejected():
    """The evaluator has nothing to grade against."""
    r = verify_goal(
        objective="ship X", workspace_dir="/ws", backlog=["item"],
    )
    assert "missing_done_when_and_no_spec" in _reject_codes(r)


def test_spec_alone_satisfies_done_when_check():
    """A spec carries acceptance criteria the chef can derive done_when from,
    so spec-only goals pass the done_when check."""
    r = verify_goal(
        objective="ship X", workspace_dir="/ws",
        spec="# spec\n## Acceptance\nfoo returns 200",
        repo_url="https://example.com/r.git",  # not from-scratch
    )
    assert "missing_done_when_and_no_spec" not in _reject_codes(r)


def test_done_when_below_min_chars_is_vague():
    r = verify_goal(
        objective="ship X", workspace_dir="/ws",
        done_when="ship it",  # 7 chars
        backlog=["item"],
    )
    assert "vague_done_when" in _reject_codes(r)


def test_done_when_at_threshold_is_accepted():
    r = verify_goal(
        objective="ship X", workspace_dir="/ws",
        done_when="x" * 20,  # exactly at the floor
        backlog=["item"],
    )
    assert "vague_done_when" not in _reject_codes(r)


def test_from_scratch_with_no_anchor_is_rejected():
    """No repo_url AND no spec AND no backlog → the chef has nothing to plan
    against. Reject."""
    r = verify_goal(
        objective="ship X", workspace_dir="/ws",
        done_when="GET /health returns HTTP 200.",
        repo_url=None,  # from-scratch
        # no spec, no backlog
    )
    assert "no_scope_anchor_for_from_scratch" in _reject_codes(r)


def test_from_scratch_with_backlog_only_is_admitted():
    r = verify_goal(
        objective="ship X", workspace_dir="/ws",
        done_when="GET /health returns HTTP 200.",
        repo_url=None,
        backlog=["scaffold the API", "add /health endpoint"],
    )
    assert r.admitted
    assert "no_scope_anchor_for_from_scratch" not in _reject_codes(r)


def test_from_scratch_with_spec_only_is_admitted():
    r = verify_goal(
        objective="ship X", workspace_dir="/ws",
        done_when="GET /health returns HTTP 200.",
        repo_url=None,
        spec="# spec\n## Scope\nin: /health endpoint",
    )
    assert r.admitted
    assert "no_scope_anchor_for_from_scratch" not in _reject_codes(r)


def test_existing_repo_needs_no_scope_anchor():
    """When repo_url is set, the investigating phase produces the discovery
    brief as the anchor — no spec/backlog required at admission time."""
    r = verify_goal(
        objective="add /health endpoint", workspace_dir="/ws",
        done_when="GET /health returns HTTP 200.",
        repo_url="https://example.com/r.git",
    )
    assert r.admitted
    assert "no_scope_anchor_for_from_scratch" not in _reject_codes(r)


def test_bare_verify_cmd_is_warning_not_rejection():
    r = verify_goal(
        objective="ship X", workspace_dir="/ws",
        done_when="GET /health returns HTTP 200.",
        repo_url="https://example.com/r.git",
        verify_cmd="pytest",
    )
    assert r.admitted  # warnings don't block
    assert "bare_verify_cmd" not in _reject_codes(r)
    assert any(c.code == "bare_verify_cmd" and c.severity == "warn" for c in r.conditions)


def test_multiple_rejections_all_surface_in_one_pass():
    """Admission collects every rejection in a single call so the waiter sees
    the full fix-list at once, not one-at-a-time."""
    r = verify_goal(
        objective="",  # missing_objective
        workspace_dir="",  # missing_workspace_dir
        # no done_when, no spec
        # no backlog, no repo_url
    )
    codes = _reject_codes(r)
    assert {
        "missing_objective", "missing_workspace_dir",
        "missing_done_when_and_no_spec", "no_scope_anchor_for_from_scratch",
    } <= codes


def test_clean_goal_is_admitted():
    r = verify_goal(
        objective="add a /health endpoint to the backend",
        workspace_dir="/ws",
        done_when="GET /health returns HTTP 200 with status:ok in the body.",
        repo_url="https://example.com/r.git",
        verify_cmd="cd backend && dotnet test",
    )
    assert r.admitted
    assert r.conditions == []


# ---- create_goal raises on rejection ----------------------------------------


def test_create_goal_raises_on_rejection(svc):
    with pytest.raises(GoalAdmissionRejected) as ei:
        svc.create_goal(
            "g-bad", objective="", workspace_dir="/ws",
        )
    # The exception carries the structured result for the boundary to surface.
    assert not ei.value.result.admitted
    assert "missing_objective" in {c.code for c in ei.value.result.rejections}


def test_create_goal_admits_valid_goal(svc):
    result = svc.create_goal(
        "g-ok",
        objective="ship the health endpoint",
        workspace_dir="/ws",
        done_when="GET /health returns HTTP 200 with status:ok in the body.",
        backlog=["add /health controller", "add test"],
    )
    # admitted → goal was created, get_goal returns its state
    assert result["id"] == "g-ok"
    assert result.get("warnings", []) == []


def test_create_goal_admits_with_warning(svc):
    result = svc.create_goal(
        "g-warn",
        objective="ship the health endpoint",
        workspace_dir="/ws",
        done_when="GET /health returns HTTP 200 with status:ok in the body.",
        backlog=["add /health controller", "add test"],
        verify_cmd="pytest",
    )
    # admitted, but the bare-tool warning is surfaced
    assert result["id"] == "g-warn"
    assert "warnings" in result
    assert any("pytest" in w and "PATH" in w for w in result["warnings"])


# ---- service.verify_goal pre-flight surface ---------------------------------


def test_service_verify_goal_returns_structured_result(svc):
    out = svc.verify_goal(objective="", workspace_dir="")
    assert out["admitted"] is False
    codes = {c["code"] for c in out["conditions"]}
    assert "missing_objective" in codes
    assert "missing_workspace_dir" in codes


def test_skills_required_unknown_rejects(tmp_path, monkeypatch):
    """When the library has skills but the caller asks for one not there,
    that's an unambiguous rejection."""
    lib = tmp_path / "skill-library"
    lib.mkdir()
    (lib / "dotnet.md").write_text("brief")
    monkeypatch.setenv("DEVCLAW_SKILL_LIBRARY", str(lib))

    r = verify_goal(
        objective="ship X", workspace_dir="/ws",
        done_when="GET /health returns 200.",
        repo_url="https://example.com/r.git",
        skills_required=["dotnet", "ghost-skill"],
    )
    assert not r.admitted
    assert "unknown_skill_required" in _reject_codes(r)


def test_skills_required_all_known_admits(tmp_path, monkeypatch):
    lib = tmp_path / "skill-library"
    lib.mkdir()
    (lib / "dotnet.md").write_text("brief")
    (lib / "react.md").write_text("brief")
    monkeypatch.setenv("DEVCLAW_SKILL_LIBRARY", str(lib))

    r = verify_goal(
        objective="ship X", workspace_dir="/ws",
        done_when="GET /health returns 200.",
        repo_url="https://example.com/r.git",
        skills_required=["dotnet", "react"],
    )
    assert r.admitted


def test_skills_required_warns_when_library_missing(tmp_path, monkeypatch):
    """Dev-env case: caller declared skills but no library on disk. Warn
    (so the operator sees it) but admit (so dev environments still work)."""
    monkeypatch.setenv("DEVCLAW_SKILL_LIBRARY", str(tmp_path / "no-such"))

    r = verify_goal(
        objective="ship X", workspace_dir="/ws",
        done_when="GET /health returns 200.",
        repo_url="https://example.com/r.git",
        skills_required=["dotnet"],
    )
    assert r.admitted  # warnings don't block
    assert any(
        c.code == "skill_library_missing" and c.severity == "warn"
        for c in r.conditions
    )


def test_skills_required_empty_is_silent(tmp_path, monkeypatch):
    """No skills requested → no skill-related conditions, regardless of
    whether the library exists."""
    monkeypatch.setenv("DEVCLAW_SKILL_LIBRARY", str(tmp_path / "no-such"))

    r = verify_goal(
        objective="ship X", workspace_dir="/ws",
        done_when="GET /health returns 200.",
        repo_url="https://example.com/r.git",
        skills_required=[],
    )
    assert r.admitted
    assert not any("skill" in c.code for c in r.conditions)


def test_service_verify_goal_does_not_persist(svc):
    """Pre-flight check must not mutate state — admission rejection must not
    accidentally leave a half-created goal on disk."""
    svc.verify_goal(
        objective="ship the health endpoint",
        workspace_dir="/ws",
        done_when="GET /health returns HTTP 200 with status:ok.",
        backlog=["x"],
    )
    # nothing got created
    assert not svc._goal_store.exists("ship the health endpoint")


# ---- standing done_when (warn, not reject) ----------------------------------


def test_standing_done_when_warns_but_admits():
    # Standing goals (closeloop-mission shape) are legitimate — the owner just
    # needs to know the done-gate will never terminally close one.
    r = verify_goal(
        objective="closeloop mirrors best-in-class CRMs",
        workspace_dir="/ws",
        done_when=(
            "Not applicable as a bounded criterion — this is a standing goal. "
            "Judge each delivery; fail any axis → off_track."
        ),
        backlog=["notifications engine"],
    )
    assert r.admitted
    assert "standing_done_when" in _codes(r)
    assert "standing_done_when" not in _reject_codes(r)


def test_bounded_done_when_has_no_standing_warning():
    r = verify_goal(
        objective="ship /health", workspace_dir="/ws",
        done_when="/health returns 200 and is covered by a passing test",
        backlog=["add /health"],
    )
    assert r.admitted
    assert "standing_done_when" not in _codes(r)
