"""Reality-anchored acceptance asserts (#2/#4, ADR 0003).

The decomposer may attach mechanical, un-fakeable acceptance checks
(``file_exists`` / ``grep``) to a checklist item; :func:`tick_settle` enforces
them against the delivered workspace so an item can't flip to ``done`` unless
the asserts actually hold in the tree — the reality anchor under the LLM review
gate (the finance-sentry-ui-library ng-zorro fake-install exhibit is the
failure class this closes). Fail-closed: an unverifiable assert fails the item;
a failing assert funnels into the same retry/circuit-breaker path as a gate
failure.

Layered like the feature: parse/dump (checklist), the pure host-side check
helpers, the async wrapper's short-circuits, the pure settle computation, and
one end-to-end wire through ``_resolve_polling_action``.
"""

from __future__ import annotations

import pytest

from devclaw.goal import checklist as _checklist
from devclaw.goal import tick_settle
from devclaw.goal.checklist import dump_checklist, parse_checklist
from devclaw.goal.models import Checklist, ChecklistItem, GoalStatus, InFlight, ItemAssert, PollResult
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import TickContext, _resolve_polling_action, _settle_addressed_items
from tests.goal_fakes import Clock, FakeClaude, FakeEngine, RecordingNotifier, seed_goal


# ---- 1. parse / validate / dump --------------------------------------------


def test_asserts_parse_valid_kinds():
    y = """
checklist:
  - id: install
    requirement: Install ng-zorro
    evidence_target: package-lock.json
    asserts:
      - {kind: file_exists, path: node_modules/ng-zorro-antd}
      - {kind: grep, path: package-lock.json, pattern: ng-zorro-antd}
      - {kind: grep, path: src/app.ts, pattern: not_yet_available, absent: true}
"""
    item = parse_checklist(y).items[0]
    assert [a.kind for a in item.asserts] == ["file_exists", "grep", "grep"]
    assert item.asserts[2].absent is True
    assert item.asserts[1].pattern == "ng-zorro-antd"


def test_asserts_drop_unsafe_and_malformed():
    # cmd kind (no arbitrary execution), absolute path, `..` traversal, a grep
    # with no pattern, and an uncompilable regex are ALL dropped — a bad assert
    # can't sink the item, and none can point outside the workspace.
    y = """
checklist:
  - id: it
    requirement: do it
    evidence_target: x
    asserts:
      - {kind: cmd, path: whatever}
      - {kind: file_exists, path: /etc/passwd}
      - {kind: grep, path: ../secrets.txt, pattern: token}
      - {kind: grep, path: real.txt}
      - {kind: grep, path: real.txt, pattern: "("}
      - {kind: file_exists, path: kept.txt}
"""
    item = parse_checklist(y).items[0]
    assert len(item.asserts) == 1
    assert item.asserts[0].kind == "file_exists"
    assert item.asserts[0].path == "kept.txt"


def test_asserts_round_trip_and_omitted_when_empty():
    cl = Checklist(items=[
        ChecklistItem(
            id="a", requirement="r", evidence_target="e",
            asserts=[
                ItemAssert(kind="file_exists", path="x/y.txt"),
                ItemAssert(kind="grep", path="lock", pattern="pkg", absent=True),
            ],
        ),
        ChecklistItem(id="b", requirement="r", evidence_target="e"),
    ])
    dumped = dump_checklist(cl)
    reparsed = parse_checklist(dumped)
    assert reparsed.items[0].asserts == cl.items[0].asserts
    # an item with no asserts carries no `asserts:` key in the view (parity with
    # the other optional fields — attempts/failure_log/scaffold).
    assert reparsed.items[1].asserts == []
    b_block = dumped.split("- id: b")[1]
    assert "asserts:" not in b_block


# ---- 2. the pure host-side check helpers -----------------------------------


def test_file_exists_assert_pass_and_fail(tmp_path):
    (tmp_path / "there.txt").write_text("hi")
    ok = ItemAssert(kind="file_exists", path="there.txt")
    missing = ItemAssert(kind="file_exists", path="gone.txt")
    assert tick_settle._check_one_assert_sync(str(tmp_path), ok) is None
    assert tick_settle._check_one_assert_sync(str(tmp_path), missing) is not None
    # absent inverts
    absent_ok = ItemAssert(kind="file_exists", path="gone.txt", absent=True)
    absent_bad = ItemAssert(kind="file_exists", path="there.txt", absent=True)
    assert tick_settle._check_one_assert_sync(str(tmp_path), absent_ok) is None
    assert tick_settle._check_one_assert_sync(str(tmp_path), absent_bad) is not None


def test_grep_assert_match_and_absent(tmp_path):
    (tmp_path / "lock.json").write_text('{"deps": {"ng-zorro-antd": "1.2.3"}}')
    hit = ItemAssert(kind="grep", path="lock.json", pattern="ng-zorro-antd")
    miss = ItemAssert(kind="grep", path="lock.json", pattern="react-router")
    assert tick_settle._check_one_assert_sync(str(tmp_path), hit) is None
    assert tick_settle._check_one_assert_sync(str(tmp_path), miss) is not None
    # absent: forbid a stub marker
    (tmp_path / "tool.cs").write_text("return NotYetAvailable();")
    forbid = ItemAssert(kind="grep", path="tool.cs", pattern="NotYetAvailable", absent=True)
    assert tick_settle._check_one_assert_sync(str(tmp_path), forbid) is not None


def test_grep_missing_file_fails_closed_but_absent_passes(tmp_path):
    # a "must match" grep on a file that isn't there FAILS CLOSED (can't prove
    # the marker); an "absent" grep on a missing file is satisfied (the thing it
    # forbids can't be present).
    must = ItemAssert(kind="grep", path="nope.txt", pattern="x")
    absent = ItemAssert(kind="grep", path="nope.txt", pattern="x", absent=True)
    assert tick_settle._check_one_assert_sync(str(tmp_path), must) is not None
    assert tick_settle._check_one_assert_sync(str(tmp_path), absent) is None


def test_assert_path_escape_fails_closed(tmp_path):
    # constructed directly (parse would have dropped it) to pin the check-time
    # re-guard: a path resolving outside the workspace root is REJECTED, never
    # silently read.
    (tmp_path.parent / "outside.txt").write_text("secret")
    escape = ItemAssert(kind="file_exists", path="../outside.txt")
    reason = tick_settle._check_one_assert_sync(str(tmp_path / "ws"), escape)
    assert reason is not None and "escapes" in reason


def test_check_item_asserts_returns_only_failing_items(tmp_path):
    (tmp_path / "present.txt").write_text("x")
    items = [
        ChecklistItem(id="pass", requirement="r", evidence_target="e",
                      asserts=[ItemAssert(kind="file_exists", path="present.txt")]),
        ChecklistItem(id="fail", requirement="r", evidence_target="e",
                      asserts=[ItemAssert(kind="file_exists", path="absent.txt")]),
        ChecklistItem(id="none", requirement="r", evidence_target="e"),  # no asserts
    ]
    failures = tick_settle._check_item_asserts_sync(str(tmp_path), items)
    assert set(failures) == {"fail"}


# ---- 3. the async wrapper's short-circuits ---------------------------------


@pytest.mark.asyncio
async def test_check_addressed_asserts_disabled_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(tick_settle, "ITEM_ASSERTS_ENABLED", False)
    cl = Checklist(items=[
        ChecklistItem(id="a", requirement="r", evidence_target="e",
                      asserts=[ItemAssert(kind="file_exists", path="absent.txt")]),
    ])
    assert await tick_settle._check_addressed_asserts(str(tmp_path), cl, ["a"]) == {}


@pytest.mark.asyncio
async def test_check_addressed_asserts_no_asserts_short_circuits(tmp_path):
    cl = Checklist(items=[ChecklistItem(id="a", requirement="r", evidence_target="e")])
    assert await tick_settle._check_addressed_asserts(str(tmp_path), cl, ["a"]) == {}
    # empty workspace path also short-circuits (never touches the fs)
    assert await tick_settle._check_addressed_asserts("", cl, ["a"]) == {}


# ---- 4. the pure settle computation honors assert_failures ------------------


def _one_item(**kw) -> Checklist:
    base = dict(id="it", requirement="do it", evidence_target="x", status="in_flight")
    base.update(kw)
    return Checklist(items=[ChecklistItem(**base)])


def test_settle_success_with_failing_assert_is_not_marked_done():
    cl = _one_item()
    poll = PollResult(terminal=True, status="done", pr_url="https://x/pr/1", gate_passed=True)
    updated = _settle_addressed_items(
        cl, ["it"], poll, {"it": "file exists: node_modules/ng-zorro-antd → missing"},
    )
    item = updated.items[0]
    # gate passed, but the tree contradicted the assert → NOT done, back to the
    # pick-pool with the failure recorded and the attempt counted.
    assert item.status == "not_started"
    assert item.attempts == 1
    assert any("acceptance assert failed" in n for n in item.failure_log)


def test_settle_success_with_passing_asserts_marks_done():
    cl = _one_item()
    poll = PollResult(terminal=True, status="done", pr_url="https://x/pr/1", gate_passed=True)
    updated = _settle_addressed_items(cl, ["it"], poll, {})  # no failures
    assert updated.items[0].status == "done"


def test_repeated_assert_failure_trips_circuit_breaker(monkeypatch):
    monkeypatch.setattr(tick_settle, "ITEM_MAX_ATTEMPTS", 3)
    cl = _one_item(attempts=2)  # two prior failures already
    poll = PollResult(terminal=True, status="done", pr_url="https://x/pr/1", gate_passed=True)
    updated = _settle_addressed_items(cl, ["it"], poll, {"it": "grep lock must match /pkg/ → no match"})
    item = updated.items[0]
    assert item.attempts == 3
    assert item.status == "blocked"
    assert "circuit breaker" in (item.evidence or "")


def test_settle_program_items_honors_assert_failures():
    cl = _one_item()
    poll = PollResult(
        terminal=True, status="done",
        tasks=[{"plan_key": "it", "status": "done", "gate_passed": True, "pr_url": "u"}],
    )
    updated = tick_settle._settle_program_items(cl, ["it"], poll, {"it": "grep → no match"})
    assert updated.items[0].status == "not_started"
    assert updated.items[0].attempts == 1


# ---- 5. end-to-end through _resolve_polling_action -------------------------


def _seed_with_workspace(tmp_path, item: ChecklistItem):
    """Seed a goal whose workspace_dir is a REAL directory, an in-flight action
    addressing ``item``, and a success poll — the shape a gate-passing dispatch
    settles into."""
    ws = tmp_path / "ws"
    ws.mkdir()
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g", workspace_dir=str(ws))
    store.write_checklist("g", Checklist(items=[item]))
    ref = InFlight("devclaw", "implement_feature", "t1", "task", "do it", addresses=[item.id])
    store.save_status("g", GoalStatus(phase="in_flight", lifecycle="executing", in_flight=ref))
    poll = PollResult(terminal=True, status="done", detail="did it",
                      pr_url="https://x/pr/1", gate_passed=True)
    ctx = TickContext(
        store=store, engine=FakeEngine(poll_result=poll),
        planner_caller=FakeClaude(), evaluator_caller=FakeClaude(), notifier=RecordingNotifier(),
    )
    return store, ws, ctx


@pytest.mark.asyncio
async def test_e2e_gate_passed_but_assert_fails_keeps_item_open(tmp_path):
    # The wire: a gate-passing settle whose acceptance assert does NOT hold in
    # the delivered tree leaves the item open (not_started) and says so loudly
    # in the goal log — a fabricated "done" never counts as done.
    item = ChecklistItem(
        id="install", requirement="install ng-zorro", evidence_target="package-lock.json",
        status="in_flight",
        asserts=[ItemAssert(kind="grep", path="package-lock.json", pattern="ng-zorro-antd")],
    )
    store, ws, ctx = _seed_with_workspace(tmp_path, item)  # no lockfile written → assert fails
    goal, status = store.load_goal("g"), store.load_status("g")

    await _resolve_polling_action("g", goal, status, ctx)

    assert store.read_checklist("g").items[0].status == "not_started"
    assert "acceptance asserts FAILED" in (tmp_path / "g" / "log.md").read_text()


@pytest.mark.asyncio
async def test_e2e_gate_passed_and_assert_holds_marks_done(tmp_path):
    item = ChecklistItem(
        id="install", requirement="install ng-zorro", evidence_target="package-lock.json",
        status="in_flight",
        asserts=[ItemAssert(kind="grep", path="package-lock.json", pattern="ng-zorro-antd")],
    )
    store, ws, ctx = _seed_with_workspace(tmp_path, item)
    (ws / "package-lock.json").write_text('{"packages": {"node_modules/ng-zorro-antd": {}}}')
    goal, status = store.load_goal("g"), store.load_status("g")

    await _resolve_polling_action("g", goal, status, ctx)

    assert store.read_checklist("g").items[0].status == "done"


# ---- 6. one-shot program: the mixed-result path (invariant-guard finding) ---
#
# In one-shot mode `_settle_program_items` grades each child INDIVIDUALLY, so a
# `done` child flips its item even when the PROGRAM aggregate terminalized
# `failed` because a SIBLING failed. The acceptance asserts must still gate that
# done child — else a fabricated item ships `done` on exactly the mixed-result
# path it hides in. Gating the assert check on aggregate `poll.status == done`
# (the pre-fix behavior) skipped it the moment any sibling failed.


def _seed_one_shot_program(tmp_path, items, poll):
    ws = tmp_path / "ws"
    ws.mkdir()
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g", mode="one_shot", workspace_dir=str(ws))
    store.write_checklist("g", Checklist(items=items))
    ref = InFlight("devclaw", "start_program", "p1", "program", "one-shot batch",
                   addresses=[i.id for i in items])
    store.save_status("g", GoalStatus(phase="in_flight", lifecycle="executing", in_flight=ref))
    ctx = TickContext(
        store=store, engine=FakeEngine(poll_result=poll),
        planner_caller=FakeClaude(), evaluator_caller=FakeClaude(), notifier=RecordingNotifier(),
    )
    return store, ws, ctx


def _mixed_program_poll():
    # aggregate FAILED (a sibling failed) — but the `install` child is `done`.
    return PollResult(
        terminal=True, status="failed", detail="one child failed", gate_passed=None,
        tasks=[
            {"plan_key": "install", "status": "done", "gate_passed": True,
             "pr_url": "https://x/pr/1", "error": None},
            {"plan_key": "other", "status": "failed", "gate_passed": False,
             "pr_url": None, "error": "build broke"},
        ],
    )


@pytest.mark.asyncio
async def test_one_shot_program_failed_aggregate_still_checks_done_childs_asserts(tmp_path):
    items = [
        ChecklistItem(id="install", requirement="install ng-zorro",
                      evidence_target="package-lock.json", status="in_flight",
                      asserts=[ItemAssert(kind="grep", path="package-lock.json", pattern="ng-zorro-antd")]),
        ChecklistItem(id="other", requirement="r", evidence_target="e", status="in_flight"),
    ]
    # no lockfile in ws → the done child's assert does NOT hold
    store, ws, ctx = _seed_one_shot_program(tmp_path, items, _mixed_program_poll())
    goal, status = store.load_goal("g"), store.load_status("g")

    await _resolve_polling_action("g", goal, status, ctx)

    install = next(i for i in store.read_checklist("g").items if i.id == "install")
    # aggregate was `failed`, so the pre-fix caller computed assert_failures={}
    # and this flipped to done anyway — the bug. Now it's held open.
    assert install.status == "not_started"
    assert install.attempts == 1
    assert any("acceptance assert failed" in n for n in install.failure_log)


@pytest.mark.asyncio
async def test_one_shot_program_done_child_with_holding_assert_still_marks_done(tmp_path):
    # the fix must not OVER-block: a done child whose assert holds still flips
    # done, even when a sibling failed the program aggregate.
    items = [
        ChecklistItem(id="install", requirement="install ng-zorro",
                      evidence_target="package-lock.json", status="in_flight",
                      asserts=[ItemAssert(kind="grep", path="package-lock.json", pattern="ng-zorro-antd")]),
        ChecklistItem(id="other", requirement="r", evidence_target="e", status="in_flight"),
    ]
    store, ws, ctx = _seed_one_shot_program(tmp_path, items, _mixed_program_poll())
    (ws / "package-lock.json").write_text('{"packages": {"node_modules/ng-zorro-antd": {}}}')
    goal, status = store.load_goal("g"), store.load_status("g")

    await _resolve_polling_action("g", goal, status, ctx)

    cl = store.read_checklist("g")
    assert next(i for i in cl.items if i.id == "install").status == "done"
    # the genuinely-failed sibling still goes back to the pool with its attempt.
    other = next(i for i in cl.items if i.id == "other")
    assert other.status == "not_started" and other.attempts == 1
