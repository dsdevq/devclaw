"""GoalStore's checklist artifact I/O — write / read / round-trip, the
absent-file fallback path the per-tick planner relies on, and the T0.4
missing-vs-corrupt distinction (missing → None; corrupt → loud)."""

from __future__ import annotations

import pytest

from devclaw.goal.checklist import dump_checklist
from devclaw.goal.models import Checklist, ChecklistItem
from devclaw.goal.store import GoalDocCorrupt, GoalStore


def _store(tmp_path) -> GoalStore:
    return GoalStore(tmp_path)


def _seed_goal_dir(store: GoalStore) -> None:
    store.create_goal(
        "g",
        objective="o",
        workspace_dir="/ws",
        done_when="d",
        backlog=["b1"],
    )


def _example() -> Checklist:
    return Checklist(
        items=[
            ChecklistItem(
                id="scaffold",
                requirement="Create the csproj.",
                evidence_target="backend/src/Foo.csproj",
                addresses_files=["backend/src/Foo.csproj"],
            ),
            ChecklistItem(
                id="wire",
                requirement="Wire the tool.",
                evidence_target="backend/src/Foo/Tool.cs",
                addresses_files=["backend/src/Foo/Tool.cs"],
                depends_on=["scaffold"],
                effort_minutes=15,
                model_tier="sonnet",
                note="GetAccountsQuery is in Modules.BankSync.",
            ),
        ],
        open_questions=["Is wealth-summary one tool or two?"],
        notes=["McpContractTests is shared — serialize."],
    )


def test_write_then_read_round_trip(tmp_path):
    store = _store(tmp_path)
    _seed_goal_dir(store)
    cl = _example()
    store.write_checklist("g", cl)
    loaded = store.read_checklist("g")
    assert loaded == cl


def test_read_returns_none_when_no_checklist_yet(tmp_path):
    store = _store(tmp_path)
    _seed_goal_dir(store)
    # Before the decomposer runs, the per-tick planner falls back to
    # backlog-driven mode — it MUST get None here, not an exception.
    assert store.read_checklist("g") is None


def test_read_raises_goal_doc_corrupt_when_file_corrupt(tmp_path):
    """A checklist that EXISTS but won't parse must raise, not read as absent —
    the old None fallback silently reverted the goal to the backlog planning
    pipeline (a different contract) with zero signal."""
    store = _store(tmp_path)
    _seed_goal_dir(store)
    (tmp_path / "g" / "checklist.yaml").write_text("not yaml: [garbage\n")
    with pytest.raises(GoalDocCorrupt) as excinfo:
        store.read_checklist("g")
    assert excinfo.value.goal_id == "g"
    assert excinfo.value.doc == "checklist.yaml"
    assert "checklist.yaml" in str(excinfo.value)


def test_read_corrupt_degrades_to_none_for_display(tmp_path):
    """The display accessor (on_corrupt='none') never raises — dashboards and
    wire reads degrade gracefully while the tick path stays loud."""
    store = _store(tmp_path)
    _seed_goal_dir(store)
    (tmp_path / "g" / "checklist.yaml").write_text("not yaml: [garbage\n")
    assert store.read_checklist("g", on_corrupt="none") is None


def test_write_checklist_is_atomic_replace(tmp_path):
    """write_checklist must never leave a partial checklist.yaml or a stray tmp
    file — the checklist IS the goal's structured plan (same tmp + os.replace
    treatment save_status got after the 2026-07-09 truncation incident)."""
    store = _store(tmp_path)
    _seed_goal_dir(store)
    cl = _example()
    store.write_checklist("g", cl)
    assert not (tmp_path / "g" / "checklist.yaml.tmp").exists()
    assert store.read_checklist("g") == cl


def test_write_creates_goal_dir_if_absent(tmp_path):
    # No prior create_goal — write_checklist should still mkdir -p.
    store = _store(tmp_path)
    store.write_checklist("brand-new-goal", _example())
    assert (tmp_path / "brand-new-goal" / "checklist.yaml").is_file()


def test_serialized_yaml_starts_with_checklist_key(tmp_path):
    store = _store(tmp_path)
    _seed_goal_dir(store)
    store.write_checklist("g", _example())
    text = (tmp_path / "g" / "checklist.yaml").read_text()
    # Schema contract: the file must lead with the `checklist:` top-level key
    # (so the parser's extract_yaml accepts it without trickery).
    assert text.lstrip().startswith("checklist:")
