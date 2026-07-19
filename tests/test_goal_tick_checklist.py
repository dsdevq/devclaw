"""The pillar-1 wiring in tick.py — decomposer-after-brief + checklist-mode
dispatch + per-item settle. The data-plane unit tests live in
test_goal_checklist.py / test_goal_decomposer.py / test_goal_store_checklist.py;
this file exercises the LIVE tick path: lifecycle transition, the dispatch
hook that flips items to in_flight, and the settle hook that flips them to
done (or back to not_started on failure) with grounded evidence."""

from __future__ import annotations

import json

import pytest

from devclaw.goal import tick_settle
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
    # PR7: the dispatch hook's writes are now row-only (mirror=False /
    # render_view=False) — its one production call site runs INSIDE the
    # dispatch transaction, which flushes mirrors after commit. This test
    # calls the helper standalone, so it flushes explicitly to check log.md.
    store.render_mirrors("g")

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
#
# PR7: this is now a PURE function — (checklist, addresses, poll) ->
# updated checklist — no store/goal_id, no write. Its production caller
# (_resolve_polling_action) reads the checklist, calls this to COMPUTE the
# update, and persists the result itself as a row-only write INSIDE the
# settle transaction (so a rolled-back settle can't leave an item settled
# for a delivery that was never actually recorded). These tests exercise
# the pure computation directly.


def test_settle_success_with_pr_and_gate_marks_done_with_evidence(tmp_path):
    checklist = _example_checklist()

    poll = PollResult(
        terminal=True, status="done",
        detail="agent summary", pr_url="https://x/pr/1", gate_passed=True,
    )
    updated = _settle_addressed_items(checklist, ["scaffold"], poll)

    item = next(i for i in updated.items if i.id == "scaffold")
    assert item.status == "done"
    assert item.evidence is not None
    # Honest-wording contract (closeloop-bench 2026-07-05): the evidence names
    # the PR's real state (checklist PRs are never auto-merged) and the gate
    # as the sandbox gate, so "PR <url> · gate=passed" can't read as
    # "merged and CI-green" downstream.
    assert "PR https://x/pr/1 (unmerged)" in item.evidence
    assert "sandbox gate=passed" in item.evidence


def test_settle_success_no_gate_still_marks_done(tmp_path):
    # review_repository tasks have gate_passed=None — still a success.
    checklist = _example_checklist()

    poll = PollResult(terminal=True, status="done", detail="", pr_url=None, gate_passed=None)
    updated = _settle_addressed_items(checklist, ["scaffold"], poll)

    item = next(i for i in updated.items if i.id == "scaffold")
    assert item.status == "done"
    # evidence non-empty so the per-item gate has a substring to verify
    assert item.evidence == "settled (no PR or gate)"


def test_settle_gate_failed_reverts_to_not_started(tmp_path):
    cl0 = _example_checklist()
    # simulate the dispatch hook having flipped it in_flight earlier
    cl0_in_flight = Checklist(
        items=[
            ChecklistItem(**{**vars(cl0.items[0]), "status": "in_flight"}),
            cl0.items[1],
        ],
    )

    poll = PollResult(terminal=True, status="done", pr_url="https://x/pr/1", gate_passed=False)
    updated = _settle_addressed_items(cl0_in_flight, ["scaffold"], poll)

    item = next(i for i in updated.items if i.id == "scaffold")
    # back in the pick-pool — planner can re-attempt with sharper instruction
    assert item.status == "not_started"
    assert item.evidence is None


def test_settle_task_failed_reverts_to_not_started(tmp_path):
    checklist = _example_checklist()

    poll = PollResult(terminal=True, status="failed", pr_url=None, gate_passed=None)
    updated = _settle_addressed_items(checklist, ["scaffold"], poll)

    item = next(i for i in updated.items if i.id == "scaffold")
    assert item.status == "not_started"


@pytest.mark.asyncio
async def test_settle_with_addresses_no_checklist_does_not_raise(tmp_path):
    """PR7 moved the "no checklist" guard from _settle_addressed_items
    itself (deleted — the function is pure now) up to its caller,
    _resolve_polling_action: pin the caller-level guarantee that an
    in-flight ref carrying `addresses` still settles cleanly when the goal
    has no checklist at all (was test_settle_addressed_items_no_checklist_noop,
    re-pointed at the integration level PR7's refactor moved this concern to)."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")  # no checklist written
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="executing",
        in_flight=InFlight("devclaw", "implement_feature", "t1", "task", "do it", addresses=["scaffold"]),
    ))
    planner = FakeClaude(json.dumps({"decision": "sleep", "note": "ok"}), role="planner")
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", pr_url="https://x/pr/1", gate_passed=True, detail="ok",
    ))

    out = await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(role="evaluator"),
        notifier=RecordingNotifier(), prepare_ws=fake_prepare,
    )

    assert out is Outcome.SLEPT  # settled cleanly, then the planner said sleep
    assert store.read_checklist("g") is None  # still no checklist — no crash


def test_settle_addressed_items_unknown_id_silently_skipped(tmp_path):
    checklist = _example_checklist()

    poll = PollResult(terminal=True, status="done", pr_url="x", gate_passed=True)
    updated = _settle_addressed_items(checklist, ["ghost", "scaffold"], poll)

    scaffold = next(i for i in updated.items if i.id == "scaffold")
    assert scaffold.status == "done"


# ---- #6 structural per-item circuit breaker --------------------------------


def test_settle_increments_attempts_on_failure(tmp_path):
    # A failed settle bumps the item's attempt counter; below the cap it stays
    # in the pick-pool so the planner can re-attempt.
    checklist = _example_checklist()
    poll = PollResult(terminal=True, status="failed", pr_url=None, gate_passed=None)
    updated = _settle_addressed_items(checklist, ["scaffold"], poll)
    item = next(i for i in updated.items if i.id == "scaffold")
    assert item.status == "not_started"
    assert item.attempts == 1


def test_settle_resets_attempts_on_success(tmp_path):
    # A proven item carries no stale failure count — a later steer that re-opens
    # it for rework starts fresh rather than pre-tripping the breaker.
    cl = Checklist(items=[
        ChecklistItem(**{**vars(_example_checklist().items[0]), "attempts": 2}),
    ])
    poll = PollResult(terminal=True, status="done", pr_url="https://x/pr/1", gate_passed=True)
    updated = _settle_addressed_items(cl, ["scaffold"], poll)
    assert updated.items[0].status == "done"
    assert updated.items[0].attempts == 0


def test_settle_trips_circuit_breaker_at_cap(tmp_path, monkeypatch):
    # At the cap the item flips to `blocked` (NOT back to the pick-pool), so the
    # planner stops re-picking a ticket that has failed N straight times.
    monkeypatch.setattr(tick_settle, "ITEM_MAX_ATTEMPTS", 3)
    cl = Checklist(items=[
        ChecklistItem(**{**vars(_example_checklist().items[0]),
                         "status": "in_flight", "attempts": 2}),
    ])
    poll = PollResult(terminal=True, status="done", pr_url="https://x/pr/1", gate_passed=False)
    updated = _settle_addressed_items(cl, ["scaffold"], poll)
    item = updated.items[0]
    assert item.attempts == 3
    assert item.status == "blocked"
    assert "circuit breaker" in (item.evidence or "")


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

    async def rec_prepare(ws, repo_url=None, branch=None):
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

    async def rec_prepare(ws, repo_url=None, branch=None):
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

    async def rec_prepare(ws, repo_url=None, branch=None):
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

    async def rec_prepare(ws, repo_url=None, branch=None):
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


# ---- scaffold flag derivation at dispatch (L3, #222) -----------------------


def _scaffold_checklist() -> Checklist:
    """Same shape as _example_checklist but the scaffold item is TAGGED
    scaffold=True (as the decomposer would for `dotnet new`)."""
    return Checklist(items=[
        ChecklistItem(
            id="scaffold", requirement="Run `dotnet new` for the csproj.",
            evidence_target="backend/src/Foo.csproj",
            addresses_files=["backend/src/Foo.csproj"], scaffold=True,
        ),
        ChecklistItem(
            id="wire-x", requirement="Wire the X tool.",
            evidence_target="backend/src/Tools/X.cs",
            addresses_files=["backend/src/Tools/X.cs"], depends_on=["scaffold"],
        ),
    ])


_ACT_ADDR_LOGIC = json.dumps({
    "decision": "act", "note": "wire it",
    "actions": [{
        "tool": "implement_feature",
        "goal": "Wire the X tool at backend/src/Tools/X.cs",
        "open_pr": True,
        "addresses": ["wire-x"],
    }],
})


@pytest.mark.asyncio
async def test_dispatch_derives_scaffold_flag_from_addressed_item(tmp_path):
    """An action addressing a scaffold-tagged item dispatches with the Action's
    scaffold flag DERIVED True — mechanism, not the planner LLM. The queue then
    reads that flag off the task row to skip the adversarial review gate."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_checklist("g", _scaffold_checklist())
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    planner = FakeClaude(_ACT_WITH_ADDRESSES, role="planner")  # addresses ["scaffold"]
    engine = FakeEngine()

    out = await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(role="evaluator"),
        notifier=RecordingNotifier(), prepare_ws=fake_prepare,
    )

    assert out is Outcome.DISPATCHED
    dispatched_action, _goal, _nu = engine.dispatched[0]
    assert dispatched_action.scaffold is True


@pytest.mark.asyncio
async def test_dispatch_does_not_scaffold_a_logic_item(tmp_path):
    """The over-tag guard at the dispatch seam: an action addressing a NON-
    scaffold item dispatches with scaffold=False, so real logic still gets
    reviewed even though a scaffold item exists elsewhere in the checklist."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_checklist("g", _scaffold_checklist())
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    planner = FakeClaude(_ACT_ADDR_LOGIC, role="planner")  # addresses ["wire-x"]
    engine = FakeEngine()

    await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(role="evaluator"),
        notifier=RecordingNotifier(), prepare_ws=fake_prepare,
    )

    dispatched_action, _goal, _nu = engine.dispatched[0]
    assert dispatched_action.scaffold is False


@pytest.mark.asyncio
async def test_dispatch_no_scaffold_in_backlog_mode(tmp_path):
    """A legacy backlog-mode goal (no checklist) dispatches non-scaffold — the
    derivation is a no-op without a checklist to read the tag from."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")  # no checklist
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    legacy_act = json.dumps({
        "decision": "act", "note": "do it",
        "actions": [{"tool": "implement_feature", "goal": "do something", "open_pr": True}],
    })
    planner = FakeClaude(legacy_act, role="planner")
    engine = FakeEngine()

    await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(role="evaluator"),
        notifier=RecordingNotifier(), prepare_ws=fake_prepare,
    )

    dispatched_action, _goal, _nu = engine.dispatched[0]
    assert dispatched_action.scaffold is False


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


@pytest.mark.asyncio
async def test_repeated_item_failure_trips_breaker_and_blocks_goal(tmp_path, monkeypatch):
    # #6: once an item has failed ITEM_MAX_ATTEMPTS straight times, the settle
    # hook trips the STRUCTURAL breaker — the item flips to `blocked`, the goal
    # is parked (blocked_kind=needs_human), and the owner is pinged — instead of
    # the planner re-picking the same failing ticket forever (the closeloop-bench
    # 2026-07-18 pattern). Replaces the planner-authored "CIRCUIT BREAKER" prose
    # that a forgetful planner sometimes never wrote.
    monkeypatch.setattr(tick_settle, "ITEM_MAX_ATTEMPTS", 3)
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    cl0 = _example_checklist()
    # scaffold has already failed twice; this settle is the 3rd (= cap) failure.
    cl_inflight = Checklist(items=[
        ChecklistItem(**{**vars(cl0.items[0]), "status": "in_flight", "attempts": 2}),
        cl0.items[1],
    ])
    store.write_checklist("g", cl_inflight)
    store.save_status("g", GoalStatus(
        phase="in_flight", lifecycle="executing",
        in_flight=InFlight(
            "devclaw", "fix_bug", "t3", "task", "Create the csproj.", addresses=["scaffold"],
        ),
    ))

    planner = FakeClaude(json.dumps({"decision": "sleep", "note": "ok"}), role="planner")
    engine = FakeEngine(poll_result=PollResult(
        terminal=True, status="done", pr_url="https://x/pr/1", gate_passed=False,
        detail="failed again",
    ))
    notifier = RecordingNotifier()

    out = await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(role="evaluator"),
        notifier=notifier, prepare_ws=fake_prepare,
    )

    assert out is Outcome.BLOCKED
    status = store.load_status("g")
    assert status.phase == "blocked"
    assert status.blocked_kind == "needs_answer"
    assert "circuit breaker" in (status.blocked_on or "")
    scaffold = next(i for i in store.read_checklist("g").items if i.id == "scaffold")
    assert scaffold.status == "blocked"
    assert scaffold.attempts == 3
    # the breaker blocked BEFORE any fresh planning (zero-token on the block
    # path) and the owner was pinged.
    assert planner.calls == 0
    assert any("circuit breaker" in m for m in notifier.sent)


# ---- cross-dispatch prior-attempts digest ----------------------------------
#
# The cross-dispatch half of the continuity gap: the planner sees the failure
# history in its context, but the WORKER's brief was authored fresh each
# dispatch, so a re-dispatched item re-discovered failed approaches one
# attempt at a time. Failed settles now append a compact note to the item's
# bounded failure_log; the dispatch seam renders those notes into the
# DISPATCHED goal text only (status `next` + the log line stay clean).


def test_failed_settle_appends_failure_note_to_item(tmp_path):
    checklist = _example_checklist()
    poll = PollResult(
        terminal=True, status="done", detail="Verify gate `pytest`: FAILED tail-of-boom",
        pr_url=None, gate_passed=False,
    )
    updated = _settle_addressed_items(checklist, ["scaffold"], poll)
    item = next(i for i in updated.items if i.id == "scaffold")
    assert item.attempts == 1
    assert len(item.failure_log) == 1
    assert "attempt 1:" in item.failure_log[0]
    assert "sandbox gate=FAILED" in item.failure_log[0]
    assert "tail-of-boom" in item.failure_log[0]

    # a second failure APPENDS (history, not overwrite)
    poll2 = PollResult(terminal=True, status="failed", detail="Error: other-boom",
                       pr_url=None, gate_passed=None)
    updated2 = _settle_addressed_items(updated, ["scaffold"], poll2)
    item2 = next(i for i in updated2.items if i.id == "scaffold")
    assert len(item2.failure_log) == 2
    assert "attempt 2:" in item2.failure_log[1] and "other-boom" in item2.failure_log[1]


def test_successful_settle_clears_failure_log_with_attempts(tmp_path):
    # a proven item carries no stale failure history — same rationale as the
    # attempts reset (a later steer that re-opens it starts fresh)
    cl0 = Checklist(items=[
        ChecklistItem(**{**vars(_example_checklist().items[0]),
                         "attempts": 2, "failure_log": ["attempt 1: x", "attempt 2: y"]}),
        _example_checklist().items[1],
    ])
    poll = PollResult(terminal=True, status="done", detail="", pr_url="https://x/pr/9",
                      gate_passed=True)
    updated = _settle_addressed_items(cl0, ["scaffold"], poll)
    item = next(i for i in updated.items if i.id == "scaffold")
    assert item.status == "done"
    assert item.failure_log == []
    assert item.attempts == 0


def test_failure_log_round_trips_through_yaml_and_is_bounded(tmp_path):
    from devclaw.goal.checklist import FAILURE_LOG_KEEP, dump_checklist, parse_checklist

    notes = [f"attempt {i}: boom-{i}" for i in range(1, FAILURE_LOG_KEEP + 3)]
    cl = Checklist(items=[
        ChecklistItem(**{**vars(_example_checklist().items[0]), "failure_log": notes}),
        _example_checklist().items[1],
    ])
    reloaded = parse_checklist(dump_checklist(cl))
    item = next(i for i in reloaded.items if i.id == "scaffold")
    # bounded on parse: only the NEWEST FAILURE_LOG_KEEP survive
    assert item.failure_log == notes[-FAILURE_LOG_KEEP:]
    # and an item with no failures serializes without the key at all
    assert "failure_log" not in dump_checklist(
        Checklist(items=[_example_checklist().items[1]])
    )


@pytest.mark.asyncio
async def test_redispatch_brief_carries_prior_attempt_digest(tmp_path):
    """The engine's dispatched goal carries the failure history; the recorded
    action (status.next) stays clean — presence AND absence."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    cl = Checklist(items=[
        ChecklistItem(**{**vars(_example_checklist().items[0]),
                         "attempts": 1,
                         "failure_log": ["attempt 1: settled failed · gate boom-approach"]}),
        _example_checklist().items[1],
    ])
    store.write_checklist("g", cl)
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    planner = FakeClaude(_ACT_WITH_ADDRESSES, role="planner")  # addresses ["scaffold"]
    engine = FakeEngine()

    out = await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(role="evaluator"),
        notifier=RecordingNotifier(), prepare_ws=fake_prepare,
    )

    assert out is Outcome.DISPATCHED
    dispatched_action, _goal, _nu = engine.dispatched[0]
    assert "PRIOR ATTEMPTS ON THIS WORK ITEM" in dispatched_action.goal
    assert "boom-approach" in dispatched_action.goal
    assert "[scaffold]" in dispatched_action.goal
    # the recorded next stays the planner's clean goal text
    status = store.load_status("g")
    assert "PRIOR ATTEMPTS" not in (status.next or "")
    assert "boom-approach" not in (status.next or "")


@pytest.mark.asyncio
async def test_dispatch_without_failures_is_byte_identical(tmp_path):
    """Blank-safe: no addressed item has failures → the dispatched goal is
    EXACTLY the planner's text, no digest section."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g")
    store.write_checklist("g", _example_checklist())
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))

    planner = FakeClaude(_ACT_WITH_ADDRESSES, role="planner")
    engine = FakeEngine()

    out = await tick_goal(
        "g", store=store, engine=engine,
        planner_caller=planner, evaluator_caller=FakeClaude(role="evaluator"),
        notifier=RecordingNotifier(), prepare_ws=fake_prepare,
    )

    assert out is Outcome.DISPATCHED
    dispatched_action, _goal, _nu = engine.dispatched[0]
    assert dispatched_action.goal == "Create the csproj at backend/src/Foo.csproj"
    assert "PRIOR ATTEMPTS" not in dispatched_action.goal
