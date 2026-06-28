"""Goal-layer durable-mind round-trips and cadence math (folded from goalclaw)."""

from __future__ import annotations

import pytest

from devclaw.goal.models import GoalStatus, InFlight
from devclaw.goal.store import GoalStore, parse_duration
from tests.goal_fakes import Clock, seed_goal


def test_parse_duration():
    assert parse_duration("90s") == 90
    assert parse_duration("30m") == 1800
    assert parse_duration("6h") == 21600
    assert parse_duration("1d") == 86400
    with pytest.raises(ValueError):
        parse_duration("nonsense")


def test_load_goal(tmp_path):
    seed_goal(tmp_path, "g1", backlog=["x", "y"])
    store = GoalStore(tmp_path)
    g = store.load_goal("g1")
    assert g.id == "g1"
    assert g.engine == "devclaw"
    assert g.workspace_dir == "/repos/demo"
    assert g.backlog == ["x", "y"]
    assert g.open_pr is True


def test_create_goal_writes_and_rejects_dupes(tmp_path):
    store = GoalStore(tmp_path)
    g = store.create_goal(
        "newg", objective="ship the thing", workspace_dir="/ws",
        done_when="it works", backlog=["a", "b"], cadence="6h",
    )
    assert g.objective == "ship the thing"
    assert store.exists("newg")
    assert store.load_goal("newg").backlog == ["a", "b"]
    with pytest.raises(FileExistsError):
        store.create_goal("newg", objective="dup", workspace_dir="/ws")


def test_create_goal_persists_stub_acceptable(tmp_path):
    # The owner's explicit opt-in for which tools may ship as stubs must
    # survive a round-trip through yaml — the done-gate reads it on every
    # evaluation, so silent loss = silent policy bypass.
    store = GoalStore(tmp_path)
    store.create_goal(
        "g", objective="ship mcp", workspace_dir="/ws",
        done_when="3 tools live, 1 stub", backlog=["scaffold"],
        stub_acceptable=["get_cashflow_report", "get_tax_lots"],
    )
    g = store.load_goal("g")
    assert g.stub_acceptable == ["get_cashflow_report", "get_tax_lots"]


def test_load_goal_defaults_stub_acceptable_to_empty_when_absent(tmp_path):
    # Legacy goals (written before this field existed) must load with an
    # empty list, which the done-gate treats as "no stubs allowed" — the
    # safe default.
    seed_goal(tmp_path, "legacy")
    g = GoalStore(tmp_path).load_goal("legacy")
    assert g.stub_acceptable == []


# ---- load_effective_goal — firmed overlay (2026-06-27 gap closure) --------


def test_load_effective_goal_returns_base_when_no_firmed_draft(tmp_path):
    """No firming has run yet → load_effective_goal is identical to load_goal.
    The default (firming disabled / new goal) path must be transparent."""
    store = GoalStore(tmp_path)
    store.create_goal(
        "g", objective="x", workspace_dir="/ws", done_when="original done_when",
        stub_acceptable=["original_tool"],
    )
    eff = store.load_effective_goal("g")
    base = store.load_goal("g")
    assert eff == base
    assert eff.done_when == "original done_when"
    assert eff.stub_acceptable == ["original_tool"]


def test_load_effective_goal_ignores_in_progress_firming_draft(tmp_path):
    """An in-flight firming draft (status=needs_owner_answers) is NOT
    authoritative — load_effective_goal must still return the base goal so
    the done-gate / evaluator don't honor an incomplete firming."""
    from devclaw.goal.firmed import FirmedGoal, SuccessCriterion, Unknown

    store = GoalStore(tmp_path)
    store.create_goal("g", objective="x", workspace_dir="/ws", done_when="original")
    in_progress = FirmedGoal(
        status="needs_owner_answers", round=1, intent="x",
        success_criteria=[SuccessCriterion(id="c1", text="firmed clause")],
        unknowns=[Unknown(id="q1", question="?")],
        stub_acceptable=["firmed_tool"],
    )
    store.write_firmed_draft("g", in_progress)
    eff = store.load_effective_goal("g")
    assert eff.done_when == "original"
    assert eff.stub_acceptable == []


def test_load_effective_goal_overlays_firmed_done_when_and_stub_acceptable(tmp_path):
    """A firmed draft (status=firmed) overlays both done_when (synthesized
    from success_criteria) and stub_acceptable onto the base goal — the
    done-gate now sees the OWNER's authorization decisions, not the original
    goal.yaml that's been firmed past."""
    from devclaw.goal.firmed import FirmedGoal, SuccessCriterion

    store = GoalStore(tmp_path)
    store.create_goal(
        "g", objective="x", workspace_dir="/ws", done_when="original done_when",
        stub_acceptable=[],
    )
    firmed = FirmedGoal(
        status="firmed", round=2, intent="x",
        success_criteria=[
            SuccessCriterion(id="c1", text="report exposes monthly cashflow"),
            SuccessCriterion(id="c2", text="get_cashflow_report tool ships"),
        ],
        stub_acceptable=["get_cashflow_report", "get_tax_lots"],
    )
    store.write_firmed_draft("g", firmed)
    eff = store.load_effective_goal("g")
    assert "monthly cashflow" in eff.done_when
    assert "get_cashflow_report tool ships" in eff.done_when
    assert " AND " in eff.done_when
    assert eff.stub_acceptable == ["get_cashflow_report", "get_tax_lots"]
    # base goal is unchanged on disk — load_goal still returns the original
    assert store.load_goal("g").done_when == "original done_when"
    assert store.load_goal("g").stub_acceptable == []


def test_load_effective_goal_overlays_firmed_verify_cmd(tmp_path):
    """A firmed draft that sets verify_cmd (e.g. firming derived the gate must
    run pytest AND playwright) overlays it onto the base goal — the done-gate
    and the agent both see the corrected gate. Closes the cf-11 churn root
    cause: without this, the agent had to invent Makefile/pytest-wrapper hacks
    to smuggle Playwright through a pytest-only verify_cmd."""
    from devclaw.goal.firmed import FirmedGoal, SuccessCriterion

    store = GoalStore(tmp_path)
    store.create_goal(
        "g", objective="x", workspace_dir="/ws", done_when="o",
        verify_cmd="pytest -q",
    )
    firmed = FirmedGoal(
        status="firmed", round=2, intent="x",
        success_criteria=[SuccessCriterion(id="c1", text="gate runs both layers")],
        verify_cmd="pytest -q && npx playwright test --reporter=list",
    )
    store.write_firmed_draft("g", firmed)
    eff = store.load_effective_goal("g")
    assert eff.verify_cmd == "pytest -q && npx playwright test --reporter=list"
    # base goal.yaml is unchanged on disk — load_goal still returns original
    assert store.load_goal("g").verify_cmd == "pytest -q"


def test_load_effective_goal_falls_back_to_base_verify_cmd_when_firmed_omits(tmp_path):
    """When firming did not specify a verify_cmd (the existing one already
    covers the criteria), the base goal's verify_cmd is preserved — empty/None
    in firmed means 'firming did not change the gate', not 'rescind it'."""
    from devclaw.goal.firmed import FirmedGoal, SuccessCriterion

    store = GoalStore(tmp_path)
    store.create_goal(
        "g", objective="x", workspace_dir="/ws", done_when="o",
        verify_cmd="pytest -q",
    )
    firmed = FirmedGoal(
        status="firmed", round=2, intent="x",
        success_criteria=[SuccessCriterion(id="c1", text="something")],
        verify_cmd=None,
    )
    store.write_firmed_draft("g", firmed)
    eff = store.load_effective_goal("g")
    assert eff.verify_cmd == "pytest -q"


def test_load_effective_goal_empty_firmed_stub_acceptable_falls_back_to_base(tmp_path):
    """When firmed stub_acceptable is empty but the base goal had explicit
    entries (set by the owner at creation), preserve the base list — empty
    in firmed means 'firming did not address stubs', not 'owner rescinded'."""
    from devclaw.goal.firmed import FirmedGoal, SuccessCriterion

    store = GoalStore(tmp_path)
    store.create_goal(
        "g", objective="x", workspace_dir="/ws", done_when="o",
        stub_acceptable=["pre_authorized_tool"],
    )
    firmed = FirmedGoal(
        status="firmed", round=2, intent="x",
        success_criteria=[SuccessCriterion(id="c1", text="something")],
        stub_acceptable=[],
    )
    store.write_firmed_draft("g", firmed)
    eff = store.load_effective_goal("g")
    assert eff.stub_acceptable == ["pre_authorized_tool"]


def test_status_roundtrip_with_eval_and_done_check(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    s = GoalStatus(
        phase="verifying",
        in_flight=InFlight("devclaw", "review_repository", "t9", "task", "verify", is_done_check=True),
        next="verifying done",
        last_plan_at="2026-06-06T12:00:00+00:00",
        inbox_cursor=2,
        deliveries_since_eval=3,
        last_eval_verdict="on_track",
        last_eval_note="progressing",
    )
    store.save_status("g1", s)
    back = store.load_status("g1")
    assert back.phase == "verifying"
    assert back.in_flight is not None
    assert back.in_flight.id == "t9"
    assert back.in_flight.is_done_check is True
    assert back.inbox_cursor == 2
    assert back.deliveries_since_eval == 3
    assert back.last_eval_verdict == "on_track"


def test_missing_status_is_default(tmp_path):
    store = GoalStore(tmp_path)
    s = store.load_status("never")
    assert s.phase == "idle"
    assert s.in_flight is None
    assert s.deliveries_since_eval == 0


def test_log_append_and_recent(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    store.append_log("g1", "first")
    store.append_log("g1", "second")
    recent = store.recent_log("g1")
    assert "first" in recent and "second" in recent
    assert recent.index("first") < recent.index("second")  # newest at bottom


def test_deliveries_roundtrip(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    store.append_delivery("g1", "add /health", "PR: #7\nAgent summary: added endpoint\nVerify: PASSED")
    store.append_delivery("g1", "add logging", "PR: #8")
    d = store.recent_deliveries("g1")
    assert "add /health" in d and "#7" in d and "add logging" in d


def test_inbox_cursor_and_steering_sources(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    store.append_steering("g1", ["focus on auth first"], source="denys")
    s0 = store.load_status("g1")  # cursor 0
    assert "focus on auth first" in store.unread_steering("g1", s0)
    cursor = store.steering_cursor("g1")
    assert cursor == 1
    s1 = GoalStatus(inbox_cursor=cursor)
    assert store.unread_steering("g1", s1) == ""
    # evaluator appends a correction → becomes fresh steering
    store.append_steering("g1", ["redo the rate limiter per-user"], source="auto-eval")
    fresh = store.unread_steering("g1", s1)
    assert "rate limiter" in fresh and "auto-eval" in fresh


def test_cadence_due(tmp_path):
    clock = Clock()
    store = GoalStore(tmp_path, now=clock)
    seed_goal(tmp_path, "g1", cadence="6h")
    goal = store.load_goal("g1")
    assert store.cadence_due(goal, GoalStatus(last_plan_at=None)) is True
    just_now = store.now_iso()
    assert store.cadence_due(goal, GoalStatus(last_plan_at=just_now)) is False
    clock.advance(6 * 3600 + 1)
    assert store.cadence_due(goal, GoalStatus(last_plan_at=just_now)) is True


def test_spec_roundtrip(tmp_path):
    store = GoalStore(tmp_path)
    seed_goal(tmp_path, "g")
    assert store.read_spec("g") == ""
    store.write_spec("g", "Build X")
    assert "Build X" in store.read_spec("g")
