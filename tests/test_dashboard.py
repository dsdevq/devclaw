"""Dashboard renderers — pure HTML, now unit-testable since presentation was
split out of server.py. Feeds each renderer fake data and asserts the key markup
(and that auth token + escaping flow through), giving the dashboard its first
real coverage."""
from __future__ import annotations

from dataclasses import dataclass

from devclaw.server import dashboard as d


@dataclass
class _Prog:
    id: str
    status: str
    created_at: int
    goal: str


def test_render_programs_lists_rows_and_nav():
    progs = [_Prog("prog-12345678", "running", 0, "build the thing")]
    html = d.render_programs(progs, version="9.9", token_qs="?token=x")
    assert "prog-123" in html and "build the thing" in html
    assert "/goals?token=x" in html and "/projects?token=x" in html
    assert "v9.9" in html


def test_render_program_detail_embeds_sse_and_escapes():
    prog = _Prog("p1", "running", 0, "<script>alert(1)</script>")
    html = d.render_program(prog, token_qs="")
    assert "/programs/p1/events" in html
    assert "&lt;script&gt;" in html  # goal text is escaped, not injected


def test_render_goals_phase_pill_and_empty_state():
    html = d.render_goals([], version="1", token_qs="")
    assert "no goals yet" in html
    html2 = d.render_goals(
        [{"id": "g1", "phase": "in_flight", "lifecycle": "executing",
          "actions_dispatched": 3, "objective": "do x"}],
        version="1", token_qs="",
    )
    assert "g1" in html2 and 'class="pill run"' in html2  # in_flight → run pill


def test_render_projects_health_pill_and_preview():
    items = [{
        "id": "todo", "name": "Todo", "health": "blocked", "status": "active",
        "goals": [{}, {}], "previewUrl": "http://h:8000", "repoUrl": "git@x/t.git",
    }]
    html = d.render_projects(items, version="1", token_qs="")
    assert 'class="pill bad"' in html  # blocked → bad
    assert "http://h:8000" in html and "todo" in html


def test_render_projects_empty_state():
    assert "no projects registered yet" in d.render_projects([], version="1", token_qs="")


def test_render_goal_detail_sections():
    data = {
        "objective": "ship it", "phase": "blocked", "lifecycle": "executing",
        "actions_dispatched": 2, "done_when": "all merged",
        "blocked_on": "need a decision", "deliveries": "shipped A",
        "recent_log": "did things", "live_events": [{"type": "edit", "source": "agent"}],
        "in_flight": None, "direction": {"verdict": "off_track", "note": "drifting", "at": "t"},
    }
    html = d.render_goal(data, "g1", token_qs="")
    assert "ship it" in html and "all merged" in html
    assert "need a decision" in html and "off_track" in html
    assert "shipped A" in html and "edit" in html


def test_helpers():
    assert d.phase_class("done") == "ok"
    assert d.health_class("working") == "run"
    assert d.preview_cell({"previewUrl": None}) == "—"
    assert d.esc("<b>") == "&lt;b&gt;"
