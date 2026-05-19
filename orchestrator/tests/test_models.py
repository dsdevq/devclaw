"""TaskSpec round-trip tests for the new contract_class + merged_at fields."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml

from orchestrator.dispatch import load_spec, persist_spec
from orchestrator.state.models import (
    Budget,
    RequesterRoute,
    TaskKind,
    TaskSpec,
    TaskStatus,
)


def _base_spec(**overrides) -> TaskSpec:
    base = dict(
        task_id="2026-05-19-test",
        created_at=dt.datetime(2026, 5, 19, tzinfo=dt.timezone.utc),
        created_by="test",
        requester_route=RequesterRoute(channel="test", to="t"),
        verbatim_intent="do a thing",
        kind=TaskKind.code,
        acceptance_criteria=["a"],
        budget=Budget(),
    )
    base.update(overrides)
    return TaskSpec(**base)


def test_taskspec_defaults_contract_class_and_merged_at_to_none() -> None:
    spec = _base_spec()
    assert spec.contract_class is None
    assert spec.merged_at is None


def test_taskspec_accepts_contract_class_and_merged_at() -> None:
    when = dt.datetime(2026, 5, 19, 12, 0, tzinfo=dt.timezone.utc)
    spec = _base_spec(contract_class="atomic", merged_at=when)
    assert spec.contract_class == "atomic"
    assert spec.merged_at == when


def test_taskspec_round_trip_through_yaml(tmp_path: Path) -> None:
    when = dt.datetime(2026, 5, 19, 14, 30, tzinfo=dt.timezone.utc)
    spec = _base_spec(
        contract_class="contract",
        merged_at=when,
        status=TaskStatus.done,
        result_summary="merged ok",
    )
    spec_path = tmp_path / "spec.yaml"
    persist_spec(spec, spec_path)

    on_disk = yaml.safe_load(spec_path.read_text())
    assert on_disk["contract_class"] == "contract"
    assert on_disk["merged_at"].startswith("2026-05-19T14:30")

    reloaded = load_spec(spec_path)
    assert reloaded.contract_class == "contract"
    assert reloaded.merged_at == when


def test_taskspec_round_trip_preserves_none_fields(tmp_path: Path) -> None:
    spec = _base_spec()
    spec_path = tmp_path / "spec.yaml"
    persist_spec(spec, spec_path)
    reloaded = load_spec(spec_path)
    assert reloaded.contract_class is None
    assert reloaded.merged_at is None
