"""Tests for the pr_review loop.

All subprocess calls (`gh`, `claude --print`) are mocked. No network access.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from orchestrator import pr_review as prr
from orchestrator.dispatch import load_spec, persist_spec
from orchestrator.state.models import (
    Budget,
    RequesterRoute,
    TaskKind,
    TaskSpec,
    TaskStatus,
)


# ─── Fixtures / helpers ─────────────────────────────────────────────────────


def _make_completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _gh_router(routes: dict) -> Callable[[list[str]], subprocess.CompletedProcess]:
    """Build a fake gh that returns the configured response for a given (cmd, subcmd) prefix."""
    def gh(args: list[str]) -> subprocess.CompletedProcess:
        for key, response in routes.items():
            if list(key) == args[: len(key)]:
                return response
        return _make_completed(stdout="", returncode=1, stderr=f"no route: {args}")
    return gh


def _make_spec(tmp_path: Path, task_id: str, **overrides) -> Path:
    base = dict(
        task_id=task_id,
        created_at=dt.datetime(2026, 5, 19, tzinfo=dt.timezone.utc),
        created_by="test",
        requester_route=RequesterRoute(channel="test", to="t"),
        verbatim_intent="Implement a thing as specified.",
        kind=TaskKind.code,
        acceptance_criteria=["test passes", "PR opens"],
        budget=Budget(),
        target_repo="dsdevq/devclaw",
        status=TaskStatus.done,
    )
    base.update(overrides)
    spec = TaskSpec(**base)
    task_dir = tmp_path / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    spec_path = task_dir / "spec.yaml"
    persist_spec(spec, spec_path)
    return spec_path


def _write_config(tmp_path: Path, **overrides) -> Path:
    cfg = {
        "watched_authors": [],
        "review_cap_per_tick": 3,
        "known_ci_noise": [
            {"check_name": "Lint (markdown + secrets)", "failure_substring": ""},
        ],
        "contract_class_heuristics": {
            "contract_paths": ["Dockerfile", "compose/**", "scripts/deploy*"],
        },
        "repos": ["dsdevq/devclaw"],
    }
    cfg.update(overrides)
    path = tmp_path / "pr_review.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


# ─── Discovery ──────────────────────────────────────────────────────────────


def test_discover_prs_filters_to_kit_branches() -> None:
    pr_list_payload = [
        {"number": 1, "title": "kit pr", "headRefName": "kit/abc",
         "baseRefName": "main", "author": {"login": "kit-bot"}, "body": ""},
        {"number": 2, "title": "human pr", "headRefName": "feature/foo",
         "baseRefName": "main", "author": {"login": "human"}, "body": ""},
        {"number": 3, "title": "dependabot", "headRefName": "dependabot/x",
         "baseRefName": "main", "author": {"login": "dependabot[bot]"}, "body": ""},
    ]
    gh = _gh_router({
        ("pr", "list"): _make_completed(stdout=json.dumps(pr_list_payload)),
    })
    result = prr.discover_prs("dsdevq/devclaw", gh=gh)
    assert [c.number for c in result] == [1]
    assert result[0].head_ref == "kit/abc"


def test_discover_prs_returns_empty_on_gh_failure() -> None:
    gh = _gh_router({
        ("pr", "list"): _make_completed(returncode=1, stderr="boom"),
    })
    assert prr.discover_prs("any/repo", gh=gh) == []


def test_author_allowed_branch_only_when_empty_or_star() -> None:
    assert prr.author_allowed("anyone", [])
    assert prr.author_allowed("anyone", ["*"])
    assert prr.author_allowed("kit", ["kit", "claude"])
    assert not prr.author_allowed("rando", ["kit"])


def test_find_spec_for_task_finds_atomic_and_project_paths(tmp_path: Path) -> None:
    life = tmp_path
    _make_spec(life, "atomic-1")
    pdir = life / "projects" / "devclaw" / "tasks" / "proj-2"
    pdir.mkdir(parents=True)
    persist_spec(
        TaskSpec(
            task_id="proj-2",
            created_at=dt.datetime(2026, 5, 19, tzinfo=dt.timezone.utc),
            created_by="t",
            requester_route=RequesterRoute(channel="test", to="x"),
            verbatim_intent="",
            kind=TaskKind.code,
        ),
        pdir / "spec.yaml",
    )

    assert prr.find_spec_for_task(life, "atomic-1") is not None
    assert prr.find_spec_for_task(life, "proj-2") is not None
    assert prr.find_spec_for_task(life, "ghost") is None


def test_task_id_from_head() -> None:
    assert prr.task_id_from_head("kit/2026-05-19-foo") == "2026-05-19-foo"
    assert prr.task_id_from_head("feature/foo") is None


# ─── Contract-class determination ───────────────────────────────────────────


def test_contract_class_defaults_to_atomic_with_no_signals() -> None:
    assert prr.determine_contract_class(None, ["src/foo.py"], ["Dockerfile"]) == "atomic"


def test_contract_class_honors_explicit_spec_field() -> None:
    assert prr.determine_contract_class("contract", ["src/foo.py"], ["Dockerfile"]) == "contract"
    assert prr.determine_contract_class("architecture", ["x"], []) == "architecture"


def test_contract_class_heuristic_match_overrides_default() -> None:
    assert prr.determine_contract_class(None, ["Dockerfile"], ["Dockerfile"]) == "contract"
    assert prr.determine_contract_class(None, ["compose/dev.yml"], ["compose/**"]) == "contract"


def test_contract_class_defense_in_depth_overrides_atomic_spec() -> None:
    # spec says atomic but diff touches a contract path → contract wins.
    assert (
        prr.determine_contract_class("atomic", ["scripts/deploy.sh"], ["scripts/deploy*"])
        == "contract"
    )


def test_diff_touches_contract_basename_match() -> None:
    # Pattern "Dockerfile" with no slash should still match "subdir/Dockerfile".
    assert prr.diff_touches_contract(["subdir/Dockerfile"], ["Dockerfile"])
    assert not prr.diff_touches_contract(["src/main.py"], ["Dockerfile"])


# ─── Pre-flight gates ───────────────────────────────────────────────────────


def test_is_mergeable() -> None:
    assert prr.is_mergeable({"mergeable": "MERGEABLE"})
    assert not prr.is_mergeable({"mergeable": "CONFLICTING"})
    assert not prr.is_mergeable({})


def test_ci_green_all_success() -> None:
    status = {"statusCheckRollup": [
        {"name": "test", "conclusion": "SUCCESS"},
        {"name": "build", "conclusion": "SUCCESS"},
    ]}
    assert prr.ci_green(status, [])


def test_ci_green_failure_not_in_noise() -> None:
    status = {"statusCheckRollup": [
        {"name": "test", "conclusion": "FAILURE"},
    ]}
    assert not prr.ci_green(status, [])


def test_ci_green_failure_in_noise_allowlist() -> None:
    status = {"statusCheckRollup": [
        {"name": "test", "conclusion": "SUCCESS"},
        {"name": "Lint (markdown + secrets)", "conclusion": "FAILURE"},
    ]}
    noise = [{"check_name": "Lint (markdown + secrets)", "failure_substring": ""}]
    assert prr.ci_green(status, noise)


def test_ci_green_pending_is_not_green() -> None:
    status = {"statusCheckRollup": [
        {"name": "test", "conclusion": "PENDING"},
    ]}
    assert not prr.ci_green(status, [])


# ─── Cognitive prompt assembly ──────────────────────────────────────────────


def test_build_review_prompt_includes_spec_and_diff() -> None:
    prompt = prr.build_review_prompt(
        spec_yaml_text="verbatim_intent: do X\nacceptance_criteria:\n  - test passes",
        pr_diff="diff --git a/foo b/foo\n+hello",
        pr_title="add hello",
        pr_body="implements X",
    )
    assert "verbatim_intent: do X" in prompt
    assert "acceptance_criteria" in prompt
    assert "diff --git" in prompt
    assert "add hello" in prompt
    assert "implements X" in prompt
    # Must instruct the runner to emit the canonical structured JSON envelope.
    assert "APPROVE" in prompt and "REJECT" in prompt and "UNCERTAIN" in prompt
    assert "risk_flags" in prompt


# ─── Verdict → action mapping (via run_pr_review) ──────────────────────────


def _wire_pr_review_calls(repo: str, pr_number: int, files: list[str], merge_state: str = "MERGEABLE"):
    """Build a gh-call recorder + router for one PR review tick."""
    calls: list[list[str]] = []
    pr_list_payload = [
        {"number": pr_number, "title": "atomic kit PR", "headRefName": f"kit/2026-05-19-x",
         "baseRefName": "main", "author": {"login": "kit-bot"}, "body": "body"},
    ]
    pr_view_payload = {
        "mergeable": merge_state,
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"name": "test", "conclusion": "SUCCESS"}],
        "files": [{"path": p} for p in files],
    }

    def gh(args: list[str]) -> subprocess.CompletedProcess:
        calls.append(args)
        if args[:2] == ["pr", "list"]:
            return _make_completed(stdout=json.dumps(pr_list_payload))
        if args[:2] == ["pr", "view"]:
            return _make_completed(stdout=json.dumps(pr_view_payload))
        if args[:2] == ["pr", "diff"]:
            return _make_completed(stdout="diff content")
        if args[:2] == ["pr", "merge"]:
            return _make_completed(stdout="merged")
        if args[:2] == ["pr", "comment"]:
            return _make_completed(stdout="commented")
        if args[:2] == ["run", "list"]:
            return _make_completed(stdout=json.dumps([
                {"status": "completed", "conclusion": "success"},
            ]))
        return _make_completed(returncode=1, stderr=f"no route: {args}")
    return gh, calls


def test_verdict_approve_triggers_merge_and_marks_spec(tmp_path: Path) -> None:
    life = tmp_path / "life"
    life.mkdir()
    spec_path = _make_spec(life, "2026-05-19-x")
    cfg = _write_config(life)
    gh, calls = _wire_pr_review_calls("dsdevq/devclaw", 42, files=["src/foo.py"])

    report = prr.run_pr_review(
        life,
        config_path=cfg,
        gh=gh,
        verdict_fn=lambda _p: prr.PrVerdict(verdict="APPROVE", reasoning="LGTM"),
    )

    assert 42 in report.merged
    merge_calls = [c for c in calls if c[:2] == ["pr", "merge"]]
    assert len(merge_calls) == 1
    assert "--squash" in merge_calls[0] and "--delete-branch" in merge_calls[0]
    reloaded = load_spec(spec_path)
    assert reloaded.merged_at is not None


def test_verdict_reject_posts_comment_and_does_not_merge(tmp_path: Path) -> None:
    life = tmp_path / "life"
    life.mkdir()
    _make_spec(life, "2026-05-19-x")
    cfg = _write_config(life)
    gh, calls = _wire_pr_review_calls("dsdevq/devclaw", 43, files=["src/foo.py"])

    report = prr.run_pr_review(
        life,
        config_path=cfg,
        gh=gh,
        verdict_fn=lambda _p: prr.PrVerdict(verdict="REJECT", reasoning="missing tests"),
    )

    assert report.merged == []
    assert any(c[:2] == ["pr", "comment"] for c in calls)
    assert not any(c[:2] == ["pr", "merge"] for c in calls)
    assert any(a.action == "comment" and a.verdict == "REJECT" for a in report.actions)


def test_verdict_uncertain_leaves_pr_open(tmp_path: Path) -> None:
    life = tmp_path / "life"
    life.mkdir()
    _make_spec(life, "2026-05-19-x")
    cfg = _write_config(life)
    gh, calls = _wire_pr_review_calls("dsdevq/devclaw", 44, files=["src/foo.py"])

    report = prr.run_pr_review(
        life,
        config_path=cfg,
        gh=gh,
        verdict_fn=lambda _p: prr.PrVerdict(verdict="UNCERTAIN", reasoning="???"),
    )

    assert report.merged == []
    assert not any(c[:2] == ["pr", "merge"] for c in calls)
    assert not any(c[:2] == ["pr", "comment"] for c in calls)
    assert any(a.action == "leave_open" for a in report.actions)


def test_contract_class_pr_is_surfaced_not_merged(tmp_path: Path) -> None:
    life = tmp_path / "life"
    life.mkdir()
    _make_spec(life, "2026-05-19-x", contract_class="atomic")  # defense-in-depth path
    cfg = _write_config(life)
    # Diff touches a contract path → should override to contract.
    gh, calls = _wire_pr_review_calls("dsdevq/devclaw", 45, files=["Dockerfile"])

    report = prr.run_pr_review(
        life,
        config_path=cfg,
        gh=gh,
        verdict_fn=lambda _p: pytest.fail("cognition must not be invoked for contract PR"),
    )

    assert report.merged == []
    assert any(a.action == "surface" for a in report.actions)
    assert any(c[:2] == ["pr", "comment"] for c in calls)


def test_skips_pr_with_no_matching_spec(tmp_path: Path) -> None:
    life = tmp_path / "life"
    life.mkdir()
    cfg = _write_config(life)
    gh, calls = _wire_pr_review_calls("dsdevq/devclaw", 99, files=["src/foo.py"])

    report = prr.run_pr_review(
        life,
        config_path=cfg,
        gh=gh,
        verdict_fn=lambda _p: pytest.fail("must not invoke cognition without spec"),
    )

    assert report.merged == []
    assert any(s["reason"] == "no spec on disk" for s in report.skipped)


def test_skips_already_merged_spec(tmp_path: Path) -> None:
    life = tmp_path / "life"
    life.mkdir()
    _make_spec(
        life, "2026-05-19-x",
        merged_at=dt.datetime(2026, 5, 18, tzinfo=dt.timezone.utc),
    )
    cfg = _write_config(life)
    gh, _ = _wire_pr_review_calls("dsdevq/devclaw", 50, files=["src/foo.py"])

    report = prr.run_pr_review(
        life,
        config_path=cfg,
        gh=gh,
        verdict_fn=lambda _p: pytest.fail("merged spec must short-circuit"),
    )
    assert any(s["reason"] == "already merged" for s in report.skipped)


def test_not_mergeable_is_skipped(tmp_path: Path) -> None:
    life = tmp_path / "life"
    life.mkdir()
    _make_spec(life, "2026-05-19-x")
    cfg = _write_config(life)
    gh, _ = _wire_pr_review_calls("dsdevq/devclaw", 51, files=["src/foo.py"], merge_state="CONFLICTING")
    report = prr.run_pr_review(
        life, config_path=cfg, gh=gh,
        verdict_fn=lambda _p: pytest.fail("must not invoke cognition for unmergeable"),
    )
    assert any(s["reason"] == "not mergeable" for s in report.skipped)


# ─── Circuit breaker ────────────────────────────────────────────────────────


def test_record_main_status_trips_on_two_consecutive_failures(tmp_path: Path) -> None:
    life = tmp_path
    state = prr.record_main_status(life, "success")
    assert not state["paused"]
    state = prr.record_main_status(life, "failure")
    assert not state["paused"]
    state = prr.record_main_status(life, "failure")
    assert state["paused"] is True
    assert state["paused_at"] is not None


def test_record_main_status_does_not_trip_when_alternating(tmp_path: Path) -> None:
    life = tmp_path
    prr.record_main_status(life, "failure")
    state = prr.record_main_status(life, "success")
    assert not state["paused"]
    state = prr.record_main_status(life, "failure")
    assert not state["paused"]


def test_paused_circuit_short_circuits_tick(tmp_path: Path) -> None:
    life = tmp_path / "life"
    life.mkdir()
    _make_spec(life, "2026-05-19-x")
    cfg = _write_config(life)
    prr.save_circuit(life, {
        "last_main_status": ["failure", "failure"],
        "paused": True,
        "paused_at": "2026-05-19T00:00:00+00:00",
    })

    def gh_no_call(args: list[str]) -> subprocess.CompletedProcess:
        pytest.fail(f"gh must not be called when circuit is paused; got {args}")

    report = prr.run_pr_review(
        life,
        config_path=cfg,
        gh=gh_no_call,
        verdict_fn=lambda _p: pytest.fail("cognition must not run when paused"),
    )
    assert report.circuit_paused is True
    assert report.merged == []


# ─── Telemetry ──────────────────────────────────────────────────────────────


def test_write_report_creates_dated_markdown(tmp_path: Path) -> None:
    life = tmp_path
    report = prr.PrReviewReport(
        generated_at="2026-05-19T12:00:00+00:00",
        considered=[{"repo": "x", "number": 1}],
        merged=[1],
        actions=[prr.PrAction(pr_number=1, repo="x", action="merge", reason="ok", verdict="APPROVE")],
    )
    path = prr.write_report(report, life)
    assert path.exists()
    assert path.parent.name == "audits"
    body = path.read_text()
    assert "considered: 1" in body
    assert "merged: 1" in body
