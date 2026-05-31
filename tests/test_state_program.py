"""State store unit tests — programs, tasks, atomic claim, event ordering."""

import pytest

from devclaw.state_store import StateStore


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def test_create_and_get_program_roundtrips(store):
    store.create_program(id="p1", goal="build a thing", workspace_dir="/ws")
    p = store.get_program("p1")
    assert p is not None
    assert p.goal == "build a thing"
    assert p.status == "planning"
    assert p.workspace_dir == "/ws"


def test_program_running_then_done_transitions(store):
    store.create_program(id="p1", goal="g", workspace_dir="/ws")
    store.mark_program_running("p1")
    assert store.get_program("p1").status == "running"
    store.mark_program_done("p1")
    p = store.get_program("p1")
    assert p.status == "done"
    assert p.completed_at is not None


def test_program_failed_is_sticky(store):
    store.create_program(id="p1", goal="g", workspace_dir="/ws")
    store.mark_program_running("p1")
    store.mark_program_failed("p1", "boom")
    assert store.get_program("p1").status == "failed"
    # done must not override a terminal failed state
    store.mark_program_done("p1")
    assert store.get_program("p1").status == "failed"


def test_create_task_with_program_and_deps_roundtrips(store):
    store.create_program(id="p1", goal="g", workspace_dir="/ws")
    store.create_task(
        id="t2",
        kind="implement_feature",
        workspace_dir="/ws",
        goal="second",
        program_id="p1",
        depends_on=["t1"],
        order_idx=1,
    )
    t = store.get_task("t2")
    assert t is not None
    assert t.program_id == "p1"
    assert t.depends_on == ["t1"]
    assert t.order_idx == 1


def test_claim_pending_is_atomic(store):
    store.create_task(id="t1", kind="fix_bug", workspace_dir="/ws", goal="g")
    assert store.claim_pending("t1") is True
    # second claim must lose the race
    assert store.claim_pending("t1") is False
    assert store.get_task("t1").status == "running"


def test_list_program_tasks_ignores_standalone(store):
    store.create_program(id="p1", goal="g", workspace_dir="/ws")
    store.create_task(id="a", kind="implement_feature", workspace_dir="/ws", goal="g", program_id="p1", order_idx=0)
    store.create_task(id="standalone", kind="implement_feature", workspace_dir="/ws", goal="g")
    tasks = store.list_program_tasks("p1")
    assert [t.id for t in tasks] == ["a"]


def test_events_append_and_order(store):
    store.create_task(id="t1", kind="implement_feature", workspace_dir="/ws", goal="g")
    id1 = store.append_event(task_id="t1", program_id=None, type="ActionEvent", source="agent", payload_json="{}")
    id2 = store.append_event(task_id="t1", program_id=None, type="ObservationEvent", source="env", payload_json="{}")
    assert id2 > id1
    evs = store.list_events(task_id="t1")
    assert [e.type for e in evs] == ["ActionEvent", "ObservationEvent"]
    # resume cursor
    after = store.list_events(task_id="t1", since_id=id1)
    assert [e.id for e in after] == [id2]
