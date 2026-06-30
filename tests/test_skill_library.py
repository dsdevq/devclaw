"""Skill library — provision per-project tech-stack skills into a workspace.

Tests the LIBRARY mechanism (list, provision, missing-library degrade). The
admission-side validation of skills_required against the library lives in
test_goal_admission.py.
"""

from __future__ import annotations

import os

import pytest

from devclaw.skill_library import (
    library_path,
    list_available,
    provision,
)


@pytest.fixture()
def fake_library(tmp_path, monkeypatch):
    """A populated library under tmp_path with two skill files."""
    lib = tmp_path / "skill-library"
    lib.mkdir()
    (lib / "dotnet.md").write_text(
        "# .NET engineering brief\n\n- xUnit for tests\n- EF Core for ORM\n"
    )
    (lib / "react.md").write_text(
        "# React engineering brief\n\n- functional components\n- Tailwind for utility CSS\n"
    )
    monkeypatch.setenv("DEVCLAW_SKILL_LIBRARY", str(lib))
    yield lib


# ---- library_path / list_available -----------------------------------------


def test_library_path_uses_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCLAW_SKILL_LIBRARY", str(tmp_path / "custom"))
    assert library_path() == tmp_path / "custom"


def test_library_path_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("DEVCLAW_SKILL_LIBRARY", raising=False)
    assert str(library_path()) == "/opt/devclaw/skill-library"


def test_list_available_returns_empty_when_library_missing(tmp_path, monkeypatch):
    """The dev-env case: no library on disk → no skills, but no crash either."""
    monkeypatch.setenv("DEVCLAW_SKILL_LIBRARY", str(tmp_path / "does-not-exist"))
    assert list_available() == []


def test_list_available_returns_slugs_sorted(fake_library):
    assert list_available() == ["dotnet", "react"]


def test_list_available_skips_underscore_prefix(fake_library):
    """``_common.md``-style internal files are not user-selectable slugs."""
    (fake_library / "_internal.md").write_text("internal")
    assert list_available() == ["dotnet", "react"]


def test_list_available_ignores_non_md(fake_library):
    (fake_library / "notes.txt").write_text("not a skill")
    (fake_library / "subdir").mkdir()
    assert list_available() == ["dotnet", "react"]


# ---- provision -------------------------------------------------------------


def test_provision_no_op_when_no_skills_requested(fake_library, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    result = provision(ws, [])
    assert result.provisioned == []
    assert result.missing == []
    assert result.library_existed is True
    assert not (ws / ".agent").exists()  # no dirs created when no work to do


def test_provision_copies_files_into_workspace(fake_library, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    result = provision(ws, ["dotnet", "react"])
    assert sorted(result.provisioned) == ["dotnet", "react"]
    assert result.missing == []
    # Files land at the workspace's per-repo skills dir where the runner's
    # catch-all picks them up.
    assert (ws / ".agent" / "skills" / "dotnet.md").read_text().startswith("# .NET engineering brief")
    assert (ws / ".agent" / "skills" / "react.md").read_text().startswith("# React engineering brief")


def test_provision_reports_missing_slugs(fake_library, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    result = provision(ws, ["dotnet", "ghost-skill"])
    assert result.provisioned == ["dotnet"]
    assert result.missing == ["ghost-skill"]
    assert not (ws / ".agent" / "skills" / "ghost-skill.md").exists()


def test_provision_when_library_missing_marks_all_missing(tmp_path, monkeypatch):
    """Dev-env case: caller declared skills, library doesn't exist. Returns
    all-missing so admission's warning surfaces it; no crash."""
    monkeypatch.setenv("DEVCLAW_SKILL_LIBRARY", str(tmp_path / "no-such"))
    ws = tmp_path / "ws"
    ws.mkdir()
    result = provision(ws, ["dotnet", "react"])
    assert result.provisioned == []
    assert result.missing == ["dotnet", "react"]
    assert result.library_existed is False
    assert not (ws / ".agent").exists()


def test_provision_is_idempotent(fake_library, tmp_path):
    """Re-running with the same skills overwrites cleanly — runner re-prep
    on every task means this gets called over and over."""
    ws = tmp_path / "ws"
    ws.mkdir()
    provision(ws, ["dotnet"])
    # Mutate the source so we can prove the second copy is the new content.
    (fake_library / "dotnet.md").write_text("# updated\n")
    provision(ws, ["dotnet"])
    assert (ws / ".agent" / "skills" / "dotnet.md").read_text() == "# updated\n"
