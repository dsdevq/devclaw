"""The pillar-1 wiring in tick.py — decomposer-after-brief + checklist-mode
dispatch + per-item settle. The data-plane unit tests live in
test_goal_checklist.py / test_goal_decomposer.py / test_goal_store_checklist.py;
this file exercises the LIVE tick path: lifecycle transition, the dispatch
hook that flips items to in_flight, and the settle hook that flips them to
done (or back to not_started on failure) with grounded evidence."""

from __future__ import annotations

import json

import pytest

from devclaw.goal.checklist import dump_checklist
from devclaw.goal.models import Checklist, ChecklistItem, GoalStatus, InFlight, PollResult
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import (
    Outcome,
    _flag_items_in_flight,
    _settle_addressed_items,
    tick_goal,
)
from tests.goal_fakes import Clock, FakeClaude, FakeEngine, RecordingNotifier, fake_prepare, seed_goal


def _store(tmp_path):
    return GoalStore(tmp_path, now=Clock())


def _example_checklist() -> Checklist:
    return Checklist(
        items=[
            ChecklistItem(
                id="scaffold", requirement="Create the csproj.",
                evidence_target="backend/src/Foo.csproj",
                addresses_files=["backend/src/Foo.csproj"],
            ),
            ChecklistItem(
                id="wire-x", requirement="Wire the X tool.",
                evidence_target="backend/src/Tools/X.cs",
                addresses_files=["backend/src/Tools/X.cs"],
                depends_on=["scaffold"],
            ),
        ],
    )


# ---- _flag_items_in_flight (the dispatch hook) -----------------------------


def test_flag_items_in_flight_marks_each_id(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_checklist("g", _example_checklist())

    _flag_items_in_flight(store, "g", ["scaffold"])

    cl = store.read_checklist("g")
    assert cl.items[0].status == "in_flight"
    assert cl.items[1].status == "not_started"


def test_flag_items_in_flight_no_checklist_noop(tmp_path):
    # legacy backlog-mode goal — no checklist file yet; helper must be safe.
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")

    _flag_items_in_flight(store, "g", ["whatever"])

    assert store.read_checklist("g") is None


def test_flag_items_in_flight_unknown_id_logs_and_skips(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_checklist("g", _example_checklist())

    _flag_items_in_flight(store, "g", ["ghost"])

    log = (tmp_path / "g" / "log.md").read_text()
    assert "unknown item" in log
    # Real items untouched.
    cl = store.read_checklist("g")
    assert all(i.status == "not_started" for i in cl.items)


def test_flag_items_in_flight_empty_list_noop(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_checklist("g", _example_checklist())

    _flag_items_in_flight(store, "g", [])

    cl = store.read_checklist("g")
    assert all(i.status == "not_started" for i in cl.items)


# ---- _settle_addressed_items (the settle hook) -----------------------------


def test_settle_success_with_pr_and_gate_marks_done_with_evidence(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_checklist("g", _example_checklist())

    poll = PollResult(
        terminal=True, status="done",
        detail="agent summary", pr_url="https://x/pr/1", gate_passed=True,
    )
    _settle_addressed_items(store, "g", ["scaffold"], poll)

    cl = store.read_checklist("g")
    item = next(i for i in cl.items if i.id == "scaffold")
    assert item.status == "done"
    assert item.evidence is not None
    assert "PR https://x/pr/1" in item.evidence
    assert "gate=passed" in item.evidence


def test_settle_success_no_gate_still_marks_done(tmp_path):
    # review_repository tasks have gate_passed=None — still a success.
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_checklist("g", _example_checklist())

    poll = PollResult(terminal=True, status="done", detail="", pr_url=None, gate_passed=None)
    _settle_addressed_items(store, "g", ["scaffold"], poll)

    cl = store.read_checklist("g")
    item = next(i for i in cl.items if i.id == "scaffold")
    assert item.status == "done"
    # evidence non-empty so the per-item gate has a substring to verify
    assert item.evidence == "settled (no PR or gate)"


def test_settle_gate_failed_reverts_to_not_started(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    cl0 = _example_checklist()
    # simulate the dispatch hook having flipped it in_flight earlier
    cl0_in_flight = Checklist(
        items=[
            ChecklistItem(**{**vars(cl0.items[0]), "status": "in_flight"}),
            cl0.items[1],
        ],
    )
    store.write_checklist("g", cl0_in_flight)

    poll = PollResult(terminal=True, status="done", pr_url="https://x/pr/1", gate_passed=False)
    _settle_addressed_items(store, "g", ["scaffold"], poll)

    cl = store.read_checklist("g")
    item = next(i for i in cl.items if i.id == "scaffold")
    # back in the pick-pool — planner can re-attempt with sharper instruction
    assert item.status == "not_started"
    assert item.evidence is None


def test_settle_task_failed_reverts_to_not_started(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_checklist("g", _example_checklist())

    poll = PollResult(terminal=True, status="failed", pr_url=None, gate_passed=None)
    _settle_addressed_items(store, "g", ["scaffold"], poll)

    cl = store.read_checklist("g")
    item = next(i for i in cl.items if i.id == "scaffold")
    assert item.status == "not_started"


def test_settle_addressed_items_no_checklist_noop(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")  # no checklist written

    poll = PollResult(terminal=True, status="done", pr_url="https://x/pr/1", gate_passed=True)
    _settle_addressed_items(store, "g", ["scaffold"], poll)  # must not raise


def test_settle_addressed_items_unknown_id_silently_skipped(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_checklist("g", _example_checklist())

    poll = PollResult(terminal=True, status="done", pr_url="x", gate_passed=True)
    _settle_addressed_items(store, "g", ["ghost", "scaffold"], poll)

    cl = store.read_checklist("g")
    scaffold = next(i for i in cl.items if i.id == "scaffold")
    assert scaffold.status == "done"


# ---- decomposer-after-brief lifecycle (the end-to-end Pillar 1 wire) -------


_INVESTIGATING_STATUS = GoalStatus(
    phase="in_flight",
    lifecycle="investigating",
    in_flight=InFlight("devclaw", "review_repository", "rev1", "task", "discovery", is_discovery=True),
)

# Briefer needs to return something non-empty so synth_ok=True.
_BRIEF = "## Current state\nThe repo has X.\n\n## Gap to good\nMissing Y.\n\n## What good looks like\n- thing"


_VALID_CHECKLIST_YAML = """\
checklist:
  - id: scaffold
    requirement: Create the csproj.
    evidence_target: backend/src/Foo.csproj
    addresses_files: [backend/src/Foo.csproj]
    depends_on: []
    status: not_started
    evidence: null
  - id: wire-x
    requirement: Wire the X tool.
    evidence_target: backend/src/Tools/X.cs
    addresses_files: [backend/src/Tools/X.cs]
    depends_on: [scaffold]
    status: not_started
    evidence: null
"""


@pytest.mark.asyncio
async def test_decompose_disabled_writes_no_checklist(tmp_path):
    # The default — production behavior until operator opts in.
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.save_status("g", _INVESTIGATING_STATUS)

    # research_caller (evaluator slot) returns the brief; decomposer caller
    # would NOT be invoked because decompose_enabled defaults to False.
    research = FakeClaude(_BRIEF, role="research")
    decomposer = FakeClaude(_VALID_CHECKLIST_YAML, role="decomposer")
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="analysis"))

    out = await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=FakeClaude(role="planner"), evaluator_caller=research,
        notifier=RecordingNotifier(), prepare_ws=fake_prepare,
        decompose_enabled=False,
        decomposer_caller=decomposer,
    )

    assert out is Outcome.ADVANCED
    assert store.read_checklist("g") is None
    assert decomposer.calls == 0  # NOT invoked


@pytest.mark.asyncio
async def test_decompose_enabled_writes_checklist_after_brief(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.save_status("g", _INVESTIGATING_STATUS)

    research = FakeClaude(_BRIEF, role="research")
    decomposer = FakeClaude(_VALID_CHECKLIST_YAML, role="decomposer")
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="analysis"))

    out = await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=FakeClaude(role="planner"), evaluator_caller=research,
        notifier=RecordingNotifier(), prepare_ws=fake_prepare,
        decompose_enabled=True,
        decomposer_caller=decomposer,
    )

    assert out is Outcome.ADVANCED
    cl = store.read_checklist("g")
    assert cl is not None
    assert [i.id for i in cl.items] == ["scaffold", "wire-x"]
    # The decomposer was given the brief + the discovery_detail as digest
    assert "Current state" in decomposer.last_prompt
    assert "analysis" in decomposer.last_prompt  # the repo_analysis went in as digest
    # The log records the count
    log = (tmp_path / "g" / "log.md").read_text()
    assert "checklist: 2 items" in log


@pytest.mark.asyncio
async def test_decompose_failure_falls_back_to_backlog_mode(tmp_path):
    # Decomposer returns garbage → the lifecycle still advances to executing,
    # just without a checklist (legacy backlog mode).
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.save_status("g", _INVESTIGATING_STATUS)

    research = FakeClaude(_BRIEF, role="research")
    decomposer = FakeClaude("not valid yaml at all", role="decomposer")
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="analysis"))

    out = await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=FakeClaude(role="planner"), evaluator_caller=research,
        notifier=RecordingNotifier(), prepare_ws=fake_prepare,
        decompose_enabled=True,
        decomposer_caller=decomposer,
    )

    assert out is Outcome.ADVANCED
    assert store.read_checklist("g") is None
    log = (tmp_path / "g" / "log.md").read_text()
    assert "decomposition failed" in log
    assert "falling back to backlog mode" in log
    # Lifecycle still flipped
    s = store.load_status("g")
    assert s.lifecycle == "executing"


@pytest.mark.asyncio
async def test_decompose_skipped_when_done_when_empty(tmp_path):
    # No done_when → no way to decompose (the decomposer has nothing to grade
    # against). Lifecycle advances; no checklist.
    store = _store(tmp_path)
    import yaml as _yaml
    (tmp_path / "g").mkdir(parents=True, exist_ok=True)
    (tmp_path / "g" / "goal.yaml").write_text(_yaml.safe_dump({
        "objective": "shape it up", "cadence": "1d", "engine": "devclaw",
        "workspace_dir": "/repo", "open_pr": True,
        "done_when": "",  # empty
        "backlog": ["one", "two"],
    }))
    store.save_status("g", _INVESTIGATING_STATUS)

    decomposer = FakeClaude(_VALID_CHECKLIST_YAML, role="decomposer")
    engine = FakeEngine(poll_result=PollResult(terminal=True, status="done", detail="analysis"))

    await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=FakeClaude(role="planner"),
        evaluator_caller=FakeClaude(_BRIEF, role="research"),
        notifier=RecordingNotifier(), prepare_ws=fake_prepare,
        decompose_enabled=True,
        decomposer_caller=decomposer,
    )

    assert decomposer.calls == 0  # skipped
    assert store.read_checklist("g") is None


# ---- dispatch-with-addresses end-to-end ------------------------------------


_ACT_WITH_ADDRESSES = json.dumps({
    "decision": "act", "note": "scaffold first",
    "actions": [{
        "tool": "implement_feature",
        "goal": "Create the csproj at backend/src/Foo.csproj",
        "open_pr": True,
        "addresses": ["scaffold"],
    }],
})


@pytest.mark.asyncio
async def test_planner_action_with_addresses_flips_item_in_flight(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_checklist("g", _example_checklist())
    # Goal is at idle/executing, ready for planner — no in-flight.
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    planner = FakeClaude(_ACT_WITH_ADDRESSES, role="planner")
    engine = FakeEngine()  # dispatch only; no poll this tick

    out = await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(role="evaluator"),
        notifier=RecordingNotifier(), prepare_ws=fake_prepare,
    )

    assert out is Outcome.DISPATCHED
    # Item flipped in_flight via the dispatch hook
    cl = store.read_checklist("g")
    scaffold = next(i for i in cl.items if i.id == "scaffold")
    assert scaffold.status == "in_flight"
    # The action's addresses are carried on the in-flight ref so settle finds them
    s = store.load_status("g")
    assert s.in_flight is not None
    assert s.in_flight.addresses == ["scaffold"]


@pytest.mark.asyncio
async def test_settled_addressed_action_flips_item_done_with_evidence(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    # Start with scaffold already in_flight (the dispatch hook would have done this)
    cl0 = _example_checklist()
    cl0_inflight = Checklist(items=[
        ChecklistItem(**{**vars(cl0.items[0]), "status": "in_flight"}),
        cl0.items[1],
    ])
    store.write_checklist("g", cl0_inflight)
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="executing",
        in_flight=InFlight(
            "devclaw", "implement_feature", "t1", "task",
            "Create the csproj.", addresses=["scaffold"],
        ),
    ))

    # planner re-runs after settle on the same tick; just say "sleep" so we focus
    # on the settle side-effect.
    planner = FakeClaude(json.dumps({"decision": "sleep", "note": "ok"}), role="planner")
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", pr_url="https://x/pr/1", gate_passed=True,
        detail="ok",
    ))

    await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(role="evaluator"),
        notifier=RecordingNotifier(), prepare_ws=fake_prepare,
    )

    cl = store.read_checklist("g")
    scaffold = next(i for i in cl.items if i.id == "scaffold")
    assert scaffold.status == "done"
    assert scaffold.evidence is not None
    assert "PR https://x/pr/1" in scaffold.evidence
    # The dependent item is now ready (scaffold is done)
    from devclaw.goal.checklist import ready_items
    ready = ready_items(cl)
    assert [i.id for i in ready] == ["wire-x"]


@pytest.mark.asyncio
async def test_dispatch_uses_goal_branch_when_checklist_exists(tmp_path):
    """Pillar 2 wiring — every item-mode dispatch checks out goal/<id> so
    subsequent items stack on prior items' commits instead of forking off
    main and re-implementing the foundation (2026-06-26 PR-fan-out failure)."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_checklist("g", _example_checklist())
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    calls: list = []

    async def rec_prepare(ws, repo_url=None, branch=None, skills_required=None):
        calls.append(branch)
        return branch or "main"

    planner = FakeClaude(_ACT_WITH_ADDRESSES, role="planner")
    engine = FakeEngine()

    await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(role="evaluator"),
        notifier=RecordingNotifier(), prepare_ws=rec_prepare,
    )

    assert "goal/g" in calls


@pytest.mark.asyncio
async def test_dispatch_uses_default_branch_when_no_checklist(tmp_path):
    """Legacy mode preserved: backlog-only goals still get the default-branch
    reset on every dispatch."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")  # no checklist
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    calls: list = []

    async def rec_prepare(ws, repo_url=None, branch=None, skills_required=None):
        calls.append(branch)
        return branch or "main"

    legacy_act = json.dumps({
        "decision": "act", "note": "do it",
        "actions": [{"tool": "implement_feature", "goal": "do something", "open_pr": True}],
    })
    planner = FakeClaude(legacy_act, role="planner")
    engine = FakeEngine()

    await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(role="evaluator"),
        notifier=RecordingNotifier(), prepare_ws=rec_prepare,
    )

    assert calls == [None]


@pytest.mark.asyncio
async def test_review_repository_dispatch_does_not_use_goal_branch(tmp_path):
    """``review_repository`` is read-only — it must run on the default branch
    even when a checklist exists."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_checklist("g", _example_checklist())
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    calls: list = []

    async def rec_prepare(ws, repo_url=None, branch=None, skills_required=None):
        calls.append(branch)
        return branch or "main"

    review_act = json.dumps({
        "decision": "act", "note": "review",
        "actions": [{
            "tool": "review_repository", "goal": "scan", "open_pr": False,
        }],
    })
    planner = FakeClaude(review_act, role="planner")
    engine = FakeEngine()

    await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(role="evaluator"),
        notifier=RecordingNotifier(), prepare_ws=rec_prepare,
    )

    assert calls == [None]


@pytest.mark.asyncio
async def test_done_gate_review_uses_goal_branch_when_checklist_exists(tmp_path):
    """The done-gate review judges done_when against the goal's accumulated
    work — must read the goal branch, not the empty default branch."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_checklist("g", _example_checklist())
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    calls: list = []

    async def rec_prepare(ws, repo_url=None, branch=None, skills_required=None):
        calls.append(branch)
        return branch or "main"

    planner = FakeClaude(
        json.dumps({"decision": "done", "note": "all items done"}),
        role="planner",
    )
    engine = FakeEngine()

    await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(role="evaluator"),
        notifier=RecordingNotifier(), prepare_ws=rec_prepare,
    )

    assert "goal/g" in calls


@pytest.mark.asyncio
async def test_settled_addressed_action_gate_failed_reverts_to_not_started(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    cl0 = _example_checklist()
    cl0_inflight = Checklist(items=[
        ChecklistItem(**{**vars(cl0.items[0]), "status": "in_flight"}),
        cl0.items[1],
    ])
    store.write_checklist("g", cl0_inflight)
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="executing",
        in_flight=InFlight(
            "devclaw", "implement_feature", "t1", "task",
            "Create the csproj.", addresses=["scaffold"],
        ),
    ))

    planner = FakeClaude(json.dumps({"decision": "sleep", "note": "ok"}), role="planner")
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", pr_url="https://x/pr/1", gate_passed=False,
        detail="agent failed verify",
    ))

    await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(role="evaluator"),
        notifier=RecordingNotifier(), prepare_ws=fake_prepare,
    )

    cl = store.read_checklist("g")
    scaffold = next(i for i in cl.items if i.id == "scaffold")
    # Back in the pick-pool, no evidence yet
    assert scaffold.status == "not_started"
    assert scaffold.evidence is None
