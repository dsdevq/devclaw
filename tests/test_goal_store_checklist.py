"""GoalStore's checklist artifact I/O — write / read / round-trip + the
absent-file fallback path the per-tick planner relies on."""

from __future__ import annotations

import pytest

from devclaw.goal.checklist import dump_checklist
from devclaw.goal.models import Checklist, ChecklistItem
from devclaw.goal.store import GoalStore


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


def test_read_returns_none_when_file_corrupt(tmp_path):
    store = _store(tmp_path)
    _seed_goal_dir(store)
    # Hand-write a busted checklist on disk — the planner treats it as absent.
    (tmp_path / "g" / "checklist.yaml").write_text("not yaml: [garbage\n")
    assert store.read_checklist("g") is None


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
