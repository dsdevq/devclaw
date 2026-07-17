"""Regression tests for the browser-gate WORKER doctrine (unit 4): the baked
skills mandate the machine-readable Playwright JSON-reporter contract the host
gate keys off, and the post-run hook warns EARLY when a UI-source change ships
with a verify_cmd that runs no browser E2E (so the agent fixes it before the
gate fails it closed).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SKILLS = _REPO / "openhands-runner" / "skills"
_HOOK = _REPO / "openhands-runner" / "hooks" / "post-run.sh"


# ---- skill doctrine content ---------------------------------------------------

def test_playwright_skill_mandates_the_json_reporter_contract():
    text = (_SKILLS / "craft" / "playwright.md").read_text(encoding="utf-8")
    assert "--reporter=json" in text
    assert "PLAYWRIGHT_JSON_OUTPUT_NAME" in text
    assert "webServer" in text
    # the gate keys off execution, not intent
    assert "0 executed" in text or "never ran" in text.lower()


def test_verify_gate_coverage_skill_points_at_the_browser_gate():
    text = (_SKILLS / "_writes-code" / "20-verify-gate-coverage.md").read_text(encoding="utf-8")
    assert "browser gate" in text.lower()
    assert "--reporter=json" in text


# ---- the hook fires on UI-source-changed-without-browser-run ------------------

def _git(ws: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(ws), *args], check=True,
                   capture_output=True, text=True)


def _seed_repo(tmp_path: Path) -> Path:
    ws = tmp_path / "repo"
    (ws / "src" / "app").mkdir(parents=True)
    comp = ws / "src" / "app" / "foo.component.ts"
    comp.write_text("export class Foo {}\n")
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@t")
    _git(ws, "config", "user.name", "t")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "baseline")
    head = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    (ws / ".devclaw-pre-head").write_text(head)
    # modify the tracked UI file in the working tree (diff vs pre_head sees it)
    comp.write_text("export class Foo { changed = 1; }\n")
    return ws


def _run_hook(ws: Path, verify_cmd: str) -> str:
    r = subprocess.run(
        ["bash", str(_HOOK), str(ws), "implement_feature", "task-1", verify_cmd],
        capture_output=True, text=True,
    )
    return r.stdout


def test_hook_warns_when_ui_changed_and_gate_has_no_browser_run(tmp_path):
    out = _run_hook(_seed_repo(tmp_path), "ng build && vitest run")
    assert "web-UI source changed" in out
    assert "browser gate will fail this CLOSED" in out


def test_hook_silent_when_verify_cmd_runs_playwright(tmp_path):
    out = _run_hook(_seed_repo(tmp_path), "ng build && npx playwright test --reporter=json")
    assert "web-UI source changed" not in out


def test_hook_silent_for_backend_only_change(tmp_path):
    ws = tmp_path / "repo"
    (ws / "backend").mkdir(parents=True)
    f = ws / "backend" / "Program.cs"
    f.write_text("class Program {}\n")
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@t")
    _git(ws, "config", "user.name", "t")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "baseline")
    head = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    (ws / ".devclaw-pre-head").write_text(head)
    f.write_text("class Program { int x; }\n")
    out = _run_hook(ws, "dotnet test")
    assert "web-UI source changed" not in out
