"""Unit tests for the trend-detection signals.

Each signal is tested in isolation — git plumbing is monkeypatched, inbox
content is written into a tmp goals dir. The orchestrator (TrendDetector) has
its own integration tests in test_trend_detector.py.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from devclaw import trend_signals
from devclaw.trend_signals import (
    D4AgentsMdStaleness,
    H4SteeringFrequency,
    R2RepeatedFixHotspot,
    SignalContext,
    _parse_git_log_name_only,
    _parse_inbox_denys_lines,
    all_signals,
)


def _ctx_per_project(workspace: Path, goals_dir: Path = Path("/tmp/no-such-dir")) -> SignalContext:
    return SignalContext(
        scope="per_project",
        workspace_dir=str(workspace),
        goal_id="g1",
        goals_dir=goals_dir,
        now_ms=int(time.time() * 1000),
    )


def _ctx_harness_self(goals_dir: Path, *, now_ms: int | None = None) -> SignalContext:
    return SignalContext(
        scope="harness_self",
        workspace_dir=None,
        goal_id=None,
        goals_dir=goals_dir,
        now_ms=now_ms if now_ms is not None else int(time.time() * 1000),
    )


# ---- parser smoke ----------------------------------------------------------


def test_parse_git_log_name_only_with_two_commits():
    out = (
        "abc123def456abc123def456abc123def456abcd\n"
        "src/foo.py\n"
        "src/bar.py\n"
        "\n"
        "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
        "src/foo.py\n"
    )
    parsed = _parse_git_log_name_only(out)
    assert len(parsed) == 2
    assert parsed[0][1] == ["src/foo.py", "src/bar.py"]
    assert parsed[1][1] == ["src/foo.py"]


def test_parse_git_log_name_only_handles_empty_output():
    assert _parse_git_log_name_only("") == []


def test_parse_inbox_denys_lines_filters_by_source(tmp_path):
    inbox = tmp_path / "inbox.md"
    inbox.write_text(
        "# g — inbox\n\n"
        "- [denys 2026-06-29T10:00:00+00:00] add tests\n"
        "- [auto-eval 2026-06-29T11:00:00+00:00] not denys-sourced, skip\n"
        "- [denys 2026-06-29T12:00:00+00:00] another correction\n"
        "- malformed line with no prefix\n"
    )
    entries = _parse_inbox_denys_lines(inbox)
    assert len(entries) == 2
    assert any("add tests" in e[1] for e in entries)
    assert any("another correction" in e[1] for e in entries)
    # ts_ms should be ascending in source order
    assert entries[0][0] < entries[1][0]


def test_parse_inbox_denys_lines_missing_file_returns_empty(tmp_path):
    assert _parse_inbox_denys_lines(tmp_path / "no-such.md") == []


# ---- R2: fix-class hotspot -------------------------------------------------


def _fake_git_returning(canned: str):
    def fake(args, cwd):
        return canned
    return fake


def test_r2_no_fire_when_no_workspace_dir():
    ctx = SignalContext(
        scope="per_project", workspace_dir=None, goal_id=None,
        goals_dir=Path("/tmp"), now_ms=0,
    )
    assert R2RepeatedFixHotspot().check(ctx).fired is False


def test_r2_no_fire_when_below_threshold(monkeypatch, tmp_path):
    # Three fix-class commits on the same file — below the threshold of 4.
    canned = (
        "a" * 40 + "\nsrc/foo.py\n\n"
        + "b" * 40 + "\nsrc/foo.py\n\n"
        + "c" * 40 + "\nsrc/foo.py\n"
    )
    monkeypatch.setattr(trend_signals, "_run_git", _fake_git_returning(canned))
    result = R2RepeatedFixHotspot().check(_ctx_per_project(tmp_path))
    assert result.fired is False
    assert result.actual_value == 3.0
    assert result.threshold_value == 4.0


def test_r2_fires_when_at_threshold(monkeypatch, tmp_path):
    # Four fix-class commits all touching src/foo.py — fires.
    canned = "\n\n".join(
        chr(ord("a") + i) * 40 + "\nsrc/foo.py" for i in range(4)
    )
    monkeypatch.setattr(trend_signals, "_run_git", _fake_git_returning(canned))
    result = R2RepeatedFixHotspot().check(_ctx_per_project(tmp_path))
    assert result.fired is True
    assert result.actual_value == 4.0
    assert result.evidence["file"] == "src/foo.py"
    assert result.evidence["fix_class_commit_count"] == 4
    assert len(result.evidence["commit_shas"]) == 4


def test_r2_picks_worst_offender_when_multiple_files(monkeypatch, tmp_path):
    canned = (
        "a" * 40 + "\nsrc/a.py\nsrc/b.py\n\n"
        + "b" * 40 + "\nsrc/a.py\n\n"
        + "c" * 40 + "\nsrc/a.py\n\n"
        + "d" * 40 + "\nsrc/a.py\n"
    )
    monkeypatch.setattr(trend_signals, "_run_git", _fake_git_returning(canned))
    result = R2RepeatedFixHotspot().check(_ctx_per_project(tmp_path))
    assert result.fired is True
    assert result.evidence["file"] == "src/a.py"


# ---- D4: AGENTS.md staleness ----------------------------------------------


def _unique_sha(n: int) -> str:
    """Generate a unique fake 40-hex-char SHA from an integer."""
    return f"{n:040x}"


def test_d4_no_fire_when_project_too_young(monkeypatch, tmp_path):
    """A 5-commit project doesn't have enough history for the signal."""
    five_shas = "\n".join(_unique_sha(i) for i in range(5))

    def fake_git(args, cwd):
        if "--" in args and "AGENTS.md" in args:
            return ""
        return five_shas

    monkeypatch.setattr(trend_signals, "_run_git", fake_git)
    result = D4AgentsMdStaleness().check(_ctx_per_project(tmp_path))
    assert result.fired is False
    assert result.actual_value == 5.0
    assert result.threshold_value == 30.0


def test_d4_fires_when_no_agents_md_in_history(monkeypatch, tmp_path):
    forty_shas = "\n".join(_unique_sha(i) for i in range(40))

    def fake_git(args, cwd):
        if "--" in args and "AGENTS.md" in args:
            return ""
        return forty_shas

    monkeypatch.setattr(trend_signals, "_run_git", fake_git)
    result = D4AgentsMdStaleness().check(_ctx_per_project(tmp_path))
    assert result.fired is True
    assert result.evidence["agents_md_exists_in_history"] is False
    assert result.evidence["commits_since_agents_md_touched"] == 40
    assert result.evidence["total_commits"] == 40


def test_d4_no_fire_when_agents_md_touched_recently(monkeypatch, tmp_path):
    """AGENTS.md touched at HEAD — commits_since == 0, well below threshold."""
    shas = [_unique_sha(i) for i in range(40)]
    head_sha = shas[0]
    all_shas = "\n".join(shas)

    def fake_git(args, cwd):
        if "--" in args and "AGENTS.md" in args:
            return head_sha
        return all_shas

    monkeypatch.setattr(trend_signals, "_run_git", fake_git)
    result = D4AgentsMdStaleness().check(_ctx_per_project(tmp_path))
    assert result.fired is False
    assert result.actual_value == 0.0


def test_d4_fires_when_agents_md_stale_with_recent_churn(monkeypatch, tmp_path):
    """AGENTS.md was touched 40 commits ago, project has had 40 commits since.

    Layout: newest-first log = [40 newer SHAs] + [old AGENTS sha]. So
    last_agents_sha is at index 40 → commits_since = 40, fires."""
    newer = [_unique_sha(i) for i in range(40)]
    old_agents_sha = _unique_sha(1000)  # unique, not in newer
    all_shas = "\n".join(newer + [old_agents_sha])

    def fake_git(args, cwd):
        if "--" in args and "AGENTS.md" in args:
            return old_agents_sha
        return all_shas

    monkeypatch.setattr(trend_signals, "_run_git", fake_git)
    result = D4AgentsMdStaleness().check(_ctx_per_project(tmp_path))
    assert result.fired is True
    assert result.evidence["commits_since_agents_md_touched"] == 40
    assert result.evidence["agents_md_exists_in_history"] is True


# ---- H4: steering frequency ------------------------------------------------


def _seed_goal_dir(
    goals_dir: Path,
    goal_id: str,
    *,
    inbox_lines: list[str] | None = None,
    yaml_mtime: datetime | None = None,
) -> None:
    d = goals_dir / goal_id
    d.mkdir(parents=True, exist_ok=True)
    yaml_path = d / "goal.yaml"
    yaml_path.write_text(f"objective: test goal {goal_id}\nworkspace_dir: /tmp/{goal_id}\n")
    if yaml_mtime is not None:
        ts = yaml_mtime.timestamp()
        import os
        os.utime(yaml_path, (ts, ts))
    if inbox_lines:
        (d / "inbox.md").write_text(
            f"# {goal_id} — inbox\n\n" + "\n".join(inbox_lines) + "\n"
        )


def test_h4_no_fire_when_goals_dir_missing(tmp_path):
    ctx = _ctx_harness_self(tmp_path / "no-such-goals")
    assert H4SteeringFrequency().check(ctx).fired is False


def test_h4_no_fire_when_too_few_goals(tmp_path):
    # Min-history guard: 4 goals < 5 required.
    goals_dir = tmp_path / "goals"
    for i in range(4):
        _seed_goal_dir(goals_dir, f"g{i}")
    result = H4SteeringFrequency().check(_ctx_harness_self(goals_dir))
    assert result.fired is False
    assert result.actual_value == 4.0
    assert result.threshold_value == 5.0


def test_h4_no_fire_when_goal_history_too_young(tmp_path):
    # 5 goals, all created 1 day ago — under the 14-day min-history floor.
    goals_dir = tmp_path / "goals"
    young = datetime.now(timezone.utc) - timedelta(days=1)
    for i in range(5):
        _seed_goal_dir(goals_dir, f"g{i}", yaml_mtime=young)
    result = H4SteeringFrequency().check(_ctx_harness_self(goals_dir))
    assert result.fired is False
    # actual is history_age_days (~1), threshold is 14
    assert result.threshold_value == 14.0


def test_h4_no_fire_when_no_steering_activity(tmp_path):
    goals_dir = tmp_path / "goals"
    old = datetime.now(timezone.utc) - timedelta(days=30)
    for i in range(5):
        _seed_goal_dir(goals_dir, f"g{i}", yaml_mtime=old)
    result = H4SteeringFrequency().check(_ctx_harness_self(goals_dir))
    assert result.fired is False
    # No actively-steered goals in either window — current_active == prior_active == 0
    assert result.actual_value == 0.0


def test_h4_fires_when_current_window_has_more_actively_steered_goals(tmp_path):
    goals_dir = tmp_path / "goals"
    old = datetime.now(timezone.utc) - timedelta(days=30)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    nine_days_ago = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat(timespec="seconds")

    # g0, g1, g2 each have 3+ denys steerings IN CURRENT WINDOW (last 7d).
    for i in range(3):
        _seed_goal_dir(
            goals_dir, f"g{i}",
            yaml_mtime=old,
            inbox_lines=[
                f"- [denys {now_iso}] correction {i}-1",
                f"- [denys {now_iso}] correction {i}-2",
                f"- [denys {now_iso}] correction {i}-3",
            ],
        )
    # g3, g4 each have 1 denys steering — below per-goal threshold, doesn't count
    # as actively-steered. Prior window has 0 actively-steered goals.
    for i in range(3, 5):
        _seed_goal_dir(
            goals_dir, f"g{i}",
            yaml_mtime=old,
            inbox_lines=[f"- [denys {nine_days_ago}] single correction"],
        )

    result = H4SteeringFrequency().check(_ctx_harness_self(goals_dir))
    assert result.fired is True
    assert result.actual_value == 3.0
    assert result.evidence["goals_with_3plus_denys_steerings_now"] == 3
    assert result.evidence["goals_with_3plus_denys_steerings_prior"] == 0


# ---- registry --------------------------------------------------------------


def test_all_signals_v1_set():
    sigs = all_signals()
    ids = {s.id for s in sigs}
    assert ids == {"R2", "D4", "H4"}
    # Per-project signals (R2, D4) vs harness-self (H4)
    by_scope = {s.scope for s in sigs}
    assert by_scope == {"per_project", "harness_self"}
