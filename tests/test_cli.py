"""The devclaw CLI — the terminal face of the control plane. Driven through
main(argv) against a tmp registry DB + goals dir (same stores the server uses),
so it exercises the real command wiring with no server, no queue, no claude."""
from __future__ import annotations

import json

import pytest

from devclaw.cli import main
from tests.goal_fakes import seed_goal


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "devclaw.db"
    goals = tmp_path / "goals"
    goals.mkdir()
    monkeypatch.setenv("DEVCLAW_DB", str(db))
    monkeypatch.setenv("DEVCLAW_GOALS_DIR", str(goals))
    return {"db": db, "goals": goals}


def test_register_and_list(env, capsys):
    assert main(["projects", "register", "todo", "Todo App", "--repo-url", "git@x/t.git"]) == 0
    capsys.readouterr()
    assert main(["projects", "list"]) == 0
    out = capsys.readouterr().out
    assert "todo" in out


def test_list_json_is_machine_readable(env, capsys):
    main(["projects", "register", "todo", "Todo App"])
    capsys.readouterr()
    main(["projects", "list", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and data[0]["id"] == "todo"
    assert data[0]["health"] == "idle"  # no goals linked yet


def test_show_unknown_returns_1(env, capsys):
    assert main(["projects", "show", "ghost"]) == 1
    assert "unknown project" in capsys.readouterr().err


def test_link_goal_shows_in_status(env, capsys):
    seed_goal(env["goals"], "g1")  # a real goal on disk (phase defaults to idle)
    main(["projects", "register", "todo", "Todo App"])
    assert main(["projects", "link", "todo", "g1"]) == 0
    capsys.readouterr()
    assert main(["projects", "show", "todo"]) == 0
    out = capsys.readouterr().out
    assert "g1" in out and "idle" in out


def test_dangling_link_is_flagged(env, capsys):
    main(["projects", "register", "todo", "Todo App"])
    main(["projects", "link", "todo", "nonexistent"])
    capsys.readouterr()
    main(["projects", "show", "todo"])
    assert "MISSING" in capsys.readouterr().out


def test_archive_then_health(env, capsys):
    main(["projects", "register", "todo", "Todo App"])
    assert main(["projects", "archive", "todo"]) == 0
    capsys.readouterr()
    main(["projects", "show", "todo", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "archived" and data["health"] == "archived"


def test_update_preview_url(env, capsys):
    main(["projects", "register", "todo", "Todo App"])
    assert main(["projects", "update", "todo", "--preview-url", "http://h:8000"]) == 0
    capsys.readouterr()
    main(["projects", "show", "todo", "--json"])
    assert json.loads(capsys.readouterr().out)["previewUrl"] == "http://h:8000"


def test_rm(env, capsys):
    main(["projects", "register", "todo", "Todo App"])
    assert main(["projects", "rm", "todo"]) == 0
    assert main(["projects", "show", "todo"]) == 1
