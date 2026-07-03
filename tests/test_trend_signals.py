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
    D1DiffVolume,
    D2FilesNotInAgentsMd,
    D3NewArchitecturalSurface,
    D4AgentsMdStaleness,
    D5ReadmeStaleness,
    D6ArchitectureStaleness,
    D7DecisionsStaleness,
    H4SteeringFrequency,
    R2RepeatedFixHotspot,
    SignalContext,
    _count_added_dep_lines,
    _new_top_level_dirs,
    _parse_git_log_name_only,
    _parse_inbox_denys_lines,
    _parse_shortstat,
    _path_or_parent_in_text,
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


# ---- D5 / D6 / D7: sibling doc-staleness signals ---------------------------
# The three new signals share D4's shape via _DocFileStaleness; we cover the
# per-signal path filter + evidence-key isolation rather than re-testing the
# streak arithmetic (which is D4's contract). One integration test per signal
# is enough to catch a mis-wired doc_path or evidence-slug regression.


@pytest.mark.parametrize(
    "signal_cls,doc_path,slug",
    [
        (D5ReadmeStaleness, "README.md", "readme_md"),
        (D6ArchitectureStaleness, "ARCHITECTURE.md", "architecture_md"),
        (D7DecisionsStaleness, "DECISIONS.md", "decisions_md"),
    ],
)
def test_new_doc_staleness_signals_fire_when_absent_with_churn(
    monkeypatch, tmp_path, signal_cls, doc_path, slug,
):
    """D5/D6/D7 fire when the tracked doc is absent AND the project has ≥30
    commits — same shape as D4's `agents_md_exists_in_history=False` path."""
    forty_shas = "\n".join(_unique_sha(i) for i in range(40))

    def fake_git(args, cwd):
        if "--" in args and doc_path in args:
            return ""
        return forty_shas

    monkeypatch.setattr(trend_signals, "_run_git", fake_git)
    result = signal_cls().check(_ctx_per_project(tmp_path))
    assert result.fired is True
    # Evidence keys are per-signal so downstream logs can inspect each doc
    # independently — this is the guard that a shared base didn't collapse
    # them into a single "commits_since_touched" bucket.
    assert result.evidence[f"{slug}_exists_in_history"] is False
    assert result.evidence[f"commits_since_{slug}_touched"] == 40
    assert result.evidence["total_commits"] == 40
    # Deeper refs surface the doc-specific git command for the retrospective
    # LLM pass; wrong doc_path here would silently mis-attribute drift.
    assert doc_path in result.deeper_refs["git_log_cmd"]


@pytest.mark.parametrize(
    "signal_cls,doc_path",
    [
        (D5ReadmeStaleness, "README.md"),
        (D6ArchitectureStaleness, "ARCHITECTURE.md"),
        (D7DecisionsStaleness, "DECISIONS.md"),
    ],
)
def test_new_doc_staleness_signals_no_fire_when_touched_recently(
    monkeypatch, tmp_path, signal_cls, doc_path,
):
    """Doc touched at HEAD → commits_since=0 → no fire (regression against
    the shared base returning the wrong SHA index)."""
    shas = [_unique_sha(i) for i in range(40)]
    head_sha = shas[0]
    all_shas = "\n".join(shas)

    def fake_git(args, cwd):
        if "--" in args and doc_path in args:
            return head_sha
        return all_shas

    monkeypatch.setattr(trend_signals, "_run_git", fake_git)
    result = signal_cls().check(_ctx_per_project(tmp_path))
    assert result.fired is False
    assert result.actual_value == 0.0


def test_new_doc_signals_stay_isolated_per_doc(monkeypatch, tmp_path):
    """When only ONE of the four docs is stale (say README missing) the
    OTHER signals must not also fire — guard against a shared _DocFileStaleness
    base accidentally sharing state."""
    forty_shas = "\n".join(_unique_sha(i) for i in range(40))
    head_sha = _unique_sha(0)

    def fake_git(args, cwd):
        # README.md: never committed → D5 fires.
        if "--" in args and "README.md" in args:
            return ""
        # AGENTS.md / ARCHITECTURE.md / DECISIONS.md: touched at HEAD → no fire.
        if "--" in args and (
            "AGENTS.md" in args or "ARCHITECTURE.md" in args or "DECISIONS.md" in args
        ):
            return head_sha
        return forty_shas

    monkeypatch.setattr(trend_signals, "_run_git", fake_git)
    ctx = _ctx_per_project(tmp_path)
    assert D5ReadmeStaleness().check(ctx).fired is True
    assert D4AgentsMdStaleness().check(ctx).fired is False
    assert D6ArchitectureStaleness().check(ctx).fired is False
    assert D7DecisionsStaleness().check(ctx).fired is False


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


# ---- D1: diff volume ------------------------------------------------------


def _ctx_with_bookmark(workspace: Path, bookmark: str | None) -> SignalContext:
    return SignalContext(
        scope="per_project", workspace_dir=str(workspace), goal_id="g",
        goals_dir=Path("/tmp/no-such"), now_ms=int(time.time() * 1000),
        bookmark=bookmark,
    )


def test_d1_no_fire_when_bookmark_missing(tmp_path):
    # Detector hasn't seeded the bookmark yet (or seeding failed).
    assert D1DiffVolume().check(_ctx_with_bookmark(tmp_path, None)).fired is False


def test_d1_no_fire_when_diff_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(trend_signals, "_run_git", lambda args, cwd: "")
    assert D1DiffVolume().check(_ctx_with_bookmark(tmp_path, "a" * 40)).fired is False


def test_d1_no_fire_when_below_threshold(monkeypatch, tmp_path):
    # 5 files, 100 lines — under both thresholds (10 files / 500 lines).
    monkeypatch.setattr(
        trend_signals, "_run_git",
        lambda args, cwd: " 5 files changed, 60 insertions(+), 40 deletions(-)\n",
    )
    result = D1DiffVolume().check(_ctx_with_bookmark(tmp_path, "a" * 40))
    assert result.fired is False


def test_d1_fires_on_files_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(
        trend_signals, "_run_git",
        lambda args, cwd: " 12 files changed, 80 insertions(+), 20 deletions(-)\n",
    )
    result = D1DiffVolume().check(_ctx_with_bookmark(tmp_path, "a" * 40))
    assert result.fired is True
    assert result.evidence["files_changed"] == 12
    assert result.evidence["lines_changed"] == 100


def test_d1_fires_on_lines_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(
        trend_signals, "_run_git",
        lambda args, cwd: " 3 files changed, 600 insertions(+), 0 deletions(-)\n",
    )
    result = D1DiffVolume().check(_ctx_with_bookmark(tmp_path, "a" * 40))
    assert result.fired is True
    assert result.evidence["lines_changed"] == 600


def test_d1_advances_bookmark_attribute_set():
    assert D1DiffVolume.advances_bookmark is True


def test_parse_shortstat_handles_insertions_only():
    f, i, d = _parse_shortstat(" 1 file changed, 3 insertions(+)\n")
    assert (f, i, d) == (1, 3, 0)


def test_parse_shortstat_handles_deletions_only():
    f, i, d = _parse_shortstat(" 2 files changed, 7 deletions(-)\n")
    assert (f, i, d) == (2, 0, 7)


def test_parse_shortstat_empty_returns_zeros():
    assert _parse_shortstat("") == (0, 0, 0)


# ---- D2: files-not-in-AGENTS.md ------------------------------------------


def test_d2_no_fire_when_agents_md_missing(monkeypatch, tmp_path):
    # No AGENTS.md in workspace → D2 stays out (D4 handles that).
    monkeypatch.setattr(trend_signals, "_run_git", lambda args, cwd: "abc")
    assert D2FilesNotInAgentsMd().check(_ctx_with_bookmark(tmp_path, "a" * 40)).fired is False


def test_d2_no_fire_when_bookmark_missing(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# AGENTS.md\nsrc/foo handles auth\n")
    assert D2FilesNotInAgentsMd().check(_ctx_with_bookmark(tmp_path, None)).fired is False


def test_d2_no_fire_when_files_referenced(monkeypatch, tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# AGENTS\n- src/foo handles auth\n- src/bar handles api\n- src/baz handles ui\n"
    )
    # 3 files each touched twice, all 3 referenced in AGENTS.md.
    fake_log = (
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "src/foo/x.py\nsrc/bar/y.py\nsrc/baz/z.py\n\n"
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
        "src/foo/x.py\nsrc/bar/y.py\nsrc/baz/z.py\n"
    )
    monkeypatch.setattr(trend_signals, "_run_git", lambda args, cwd: fake_log)
    result = D2FilesNotInAgentsMd().check(_ctx_with_bookmark(tmp_path, "a" * 40))
    assert result.fired is False


def test_d2_fires_when_three_files_unreferenced(monkeypatch, tmp_path):
    (tmp_path / "AGENTS.md").write_text("# AGENTS.md\nThis project does stuff.\n")
    # 3 files each touched twice — none mentioned in AGENTS.md.
    fake_log = (
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "ops/agent.py\ncore/lib.py\nweb/router.py\n\n"
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
        "ops/agent.py\ncore/lib.py\nweb/router.py\n"
    )
    monkeypatch.setattr(trend_signals, "_run_git", lambda args, cwd: fake_log)
    result = D2FilesNotInAgentsMd().check(_ctx_with_bookmark(tmp_path, "a" * 40))
    assert result.fired is True
    assert result.evidence["unreferenced_files_count"] == 3
    files = {f["file"] for f in result.evidence["top_files"]}
    assert files == {"ops/agent.py", "core/lib.py", "web/router.py"}


def test_d2_skips_files_touched_only_once(monkeypatch, tmp_path):
    """A single-commit touch isn't enough — D2 wants repeated activity."""
    (tmp_path / "AGENTS.md").write_text("# AGENTS.md\n")
    fake_log = (
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "ops/a.py\ncore/b.py\nweb/c.py\n"
    )
    monkeypatch.setattr(trend_signals, "_run_git", lambda args, cwd: fake_log)
    result = D2FilesNotInAgentsMd().check(_ctx_with_bookmark(tmp_path, "a" * 40))
    assert result.fired is False


def test_d2_advances_bookmark_attribute_set():
    assert D2FilesNotInAgentsMd.advances_bookmark is True


def test_path_or_parent_in_text_matches_substrings():
    txt = "AGENTS.md\n- src/foo handles auth"
    assert _path_or_parent_in_text("src/foo/bar.py", txt) is True
    assert _path_or_parent_in_text("src/foo.py", txt) is True
    assert _path_or_parent_in_text("lib/x.py", txt) is False


# ---- D3: new dir / new dep ------------------------------------------------


def test_d3_no_fire_when_nothing_added(monkeypatch, tmp_path):
    monkeypatch.setattr(trend_signals, "_run_git", lambda args, cwd: "")
    assert D3NewArchitecturalSurface().check(_ctx_with_bookmark(tmp_path, "a" * 40)).fired is False


def test_d3_no_fire_when_bookmark_missing(tmp_path):
    assert D3NewArchitecturalSurface().check(_ctx_with_bookmark(tmp_path, None)).fired is False


def test_d3_fires_on_new_top_level_dir(monkeypatch, tmp_path):
    def fake_git(args, cwd):
        if "--diff-filter=A" in args:
            return "ops/agent.py\nops/signals.py\n"
        return ""  # no dep file diffs
    monkeypatch.setattr(trend_signals, "_run_git", fake_git)
    result = D3NewArchitecturalSurface().check(_ctx_with_bookmark(tmp_path, "a" * 40))
    assert result.fired is True
    assert "ops" in result.evidence["new_directories"]


def test_d3_fires_on_new_dep_in_requirements(monkeypatch, tmp_path):
    (tmp_path / "requirements.txt").write_text("existing-dep==1.0\n")

    def fake_git(args, cwd):
        if "--diff-filter=A" in args:
            return ""
        if "requirements.txt" in args:
            return (
                "diff --git a/requirements.txt b/requirements.txt\n"
                "+httpx>=0.27\n+pyyaml>=6\n"
            )
        return ""
    monkeypatch.setattr(trend_signals, "_run_git", fake_git)
    result = D3NewArchitecturalSurface().check(_ctx_with_bookmark(tmp_path, "a" * 40))
    assert result.fired is True
    assert result.evidence["new_deps_per_file"]["requirements.txt"] == 2


def test_d3_ignores_diff_noise(monkeypatch, tmp_path):
    """The +++ header and pure-comment additions aren't counted as deps."""
    (tmp_path / "requirements.txt").write_text("# header\n")

    def fake_git(args, cwd):
        if "--diff-filter=A" in args:
            return ""
        if "requirements.txt" in args:
            return (
                "+++ b/requirements.txt\n"
                "+# a comment line\n"
                "+   \n"
            )
        return ""
    monkeypatch.setattr(trend_signals, "_run_git", fake_git)
    result = D3NewArchitecturalSurface().check(_ctx_with_bookmark(tmp_path, "a" * 40))
    assert result.fired is False


def test_new_top_level_dirs_ignores_top_level_files():
    nd = _new_top_level_dirs(["README.md", "ops/a.py", "Makefile", "src/b/c.py"])
    assert nd == {"ops", "src"}


def test_count_added_dep_lines_ignores_comments_and_blanks():
    diff = (
        "+++ b/requirements.txt\n"
        "+httpx>=0.27\n"
        "+# comment\n"
        "+\n"
        "+pyyaml>=6\n"
    )
    assert _count_added_dep_lines(diff) == 2


# ---- registry --------------------------------------------------------------


def test_all_signals_set():
    sigs = all_signals()
    ids = {s.id for s in sigs}
    assert ids == {"R2", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "H4"}
    by_scope = {s.scope for s in sigs}
    assert by_scope == {"per_project", "harness_self"}
    # Bookmark-aware signals are exactly D1/D2/D3 in this PR.
    bookmark_aware = {s.id for s in sigs if s.advances_bookmark}
    assert bookmark_aware == {"D1", "D2", "D3"}


# ---- fingerprint (mute mechanism, 2026-07-03 audit fix) --------------------


def _fake_signal_result(evidence: dict, fired: bool = True):
    """Build a SignalResult without importing the trend_signals internals."""
    from devclaw.trend_signals import SignalResult
    return SignalResult(fired=fired, evidence=evidence)


def test_r2_fingerprint_stable_across_ticks_with_same_evidence():
    """R2 fingerprint is identity of (file, sorted-SHA-set). Two fires with
    the same file + same SHA set → same fingerprint → mute activates.
    Regression against the 4-day-in-a-row R2 fires on closeloop-frontend-refactor
    2026-06-29 through 2026-07-02 that the audit surfaced."""
    r2 = R2RepeatedFixHotspot()
    ev = {
        "file": "AGENTS.md",
        "fix_class_commit_count": 7,
        "commit_shas": ["a1", "b2", "c3", "d4", "e5", "f6", "07"],
        "window_days": 30,
    }
    fp1 = r2.fingerprint(_fake_signal_result(ev))
    fp2 = r2.fingerprint(_fake_signal_result(ev))
    assert fp1 == fp2
    # SHA ORDER doesn't matter — we sort before hashing.
    ev_reordered = {**ev, "commit_shas": ["07", "f6", "e5", "d4", "c3", "b2", "a1"]}
    assert r2.fingerprint(_fake_signal_result(ev_reordered)) == fp1


def test_r2_fingerprint_differs_when_new_fix_commit_lands():
    """A new fix-class commit on the hot file adds a SHA to commit_shas —
    fingerprint MUST differ so the fire refires on genuinely fresh evidence."""
    r2 = R2RepeatedFixHotspot()
    base = {"file": "AGENTS.md", "commit_shas": ["a1", "b2", "c3", "d4"]}
    later = {"file": "AGENTS.md", "commit_shas": ["a1", "b2", "c3", "d4", "e5"]}
    assert r2.fingerprint(_fake_signal_result(base)) != r2.fingerprint(
        _fake_signal_result(later)
    )


def test_r2_fingerprint_differs_when_hot_file_moves():
    """Different hot file = different story = different fingerprint. Prevents
    the (unlikely but valid) case of two files sharing the same SHA set."""
    r2 = R2RepeatedFixHotspot()
    a = {"file": "AGENTS.md", "commit_shas": ["a1", "b2", "c3", "d4"]}
    b = {"file": "README.md", "commit_shas": ["a1", "b2", "c3", "d4"]}
    assert r2.fingerprint(_fake_signal_result(a)) != r2.fingerprint(_fake_signal_result(b))


def test_doc_staleness_fingerprint_stable_when_last_touched_sha_unchanged():
    """D4/D5/D6/D7 fingerprint hangs off the last-touched SHA. The commits-
    since counter grows every commit but the SHA is what defines "situation."
    Same SHA across ticks → same fingerprint → mute suppresses. Prevents the
    D5 README-staleness pattern from firing every day as the counter grows."""
    d5 = D5ReadmeStaleness()
    ev_today = {
        "commits_since_readme_md_touched": 40,
        "total_commits": 54,
        "last_readme_md_sha": "0a109113b3a5e78f6f98e554375622354031c1bb",
        "readme_md_exists_in_history": True,
    }
    ev_tomorrow_10_more_commits = {
        **ev_today,
        "commits_since_readme_md_touched": 50,
        "total_commits": 64,
    }
    fp1 = d5.fingerprint(_fake_signal_result(ev_today))
    fp2 = d5.fingerprint(_fake_signal_result(ev_tomorrow_10_more_commits))
    # SAME fingerprint even though the counter grew — because the DOC-touched
    # SHA is unchanged. The "story" is still "README hasn't been touched since sha X."
    assert fp1 == fp2


def test_doc_staleness_fingerprint_differs_when_doc_finally_touched():
    """When the doc is finally touched, last_<doc>_sha changes → fingerprint
    changes → if the signal still fires (threshold still crossed for a
    different reason), it fires fresh, not muted."""
    d5 = D5ReadmeStaleness()
    old = {"last_readme_md_sha": "aaa", "readme_md_exists_in_history": True}
    new = {"last_readme_md_sha": "bbb", "readme_md_exists_in_history": True}
    assert d5.fingerprint(_fake_signal_result(old)) != d5.fingerprint(_fake_signal_result(new))


def test_doc_staleness_fingerprint_differs_when_doc_first_created():
    """Special case: doc previously didn't exist (last SHA = None), then
    someone created it (last SHA becomes a real SHA). Fingerprint must
    differ — the story "doc never existed" is genuinely different from
    "doc exists but was last touched at SHA X"."""
    d5 = D5ReadmeStaleness()
    never = {"last_readme_md_sha": None, "readme_md_exists_in_history": False}
    now_exists = {"last_readme_md_sha": "abc", "readme_md_exists_in_history": True}
    assert d5.fingerprint(_fake_signal_result(never)) != d5.fingerprint(
        _fake_signal_result(now_exists)
    )


def test_default_fingerprint_hashes_full_evidence():
    """Signals that don't override ``fingerprint`` get the default: stable
    hash over sorted evidence keys+values. Same evidence dict → same fp;
    any change → different fp. Catches signals that don't yet override
    (H4 today, future signals tomorrow) and ensures they mute correctly
    when evidence is verbatim-identical."""
    from devclaw.trend_signals import Signal

    class _ProbeSignal(Signal):
        id = "PROBE"

    sig = _ProbeSignal()
    ev1 = {"a": 1, "b": [2, 3], "c": "x"}
    ev2 = {"a": 1, "b": [2, 3], "c": "x"}
    assert sig.fingerprint(_fake_signal_result(ev1)) == sig.fingerprint(
        _fake_signal_result(ev2)
    )
    ev3 = {"a": 1, "b": [2, 3, 4], "c": "x"}  # one extra list element
    assert sig.fingerprint(_fake_signal_result(ev1)) != sig.fingerprint(
        _fake_signal_result(ev3)
    )
