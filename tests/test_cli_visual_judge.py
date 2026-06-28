"""CLI: `devclaw visual-judge <workspace>` — drives the gate against an existing
workspace checkpoint so the rubric + prompt can be tuned without a full task.

Stubs ``judge_screenshots`` so no real claude call fires; exercises the script-
runner + exit-code semantics + JSON output mode end to end.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from devclaw import cli


def _install_script(workspace: Path, body: str, *, executable: bool = True) -> Path:
    agent = workspace / ".agent"
    agent.mkdir(exist_ok=True)
    script = agent / "visual-verify.sh"
    script.write_text(body)
    if executable:
        st = script.stat().st_mode
        script.chmod(st | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _patch_judge(monkeypatch, verdict: dict):
    async def fake_judge(**_kw):
        return verdict
    monkeypatch.setattr("devclaw.quality.visual_judge.judge_screenshots", fake_judge)


_MANIFEST_JSON = json.dumps({
    "routes": [{"label": "home", "url": "/", "screenshot": "home.png"}],
    "notes": "captured",
})


def test_workspace_not_found_returns_infra_error(tmp_path, capsys):
    rc = cli.main(["visual-judge", str(tmp_path / "nope")])
    assert rc == 2
    assert "workspace not found" in capsys.readouterr().err


def test_no_script_returns_infra_error(tmp_path, capsys):
    rc = cli.main(["visual-judge", str(tmp_path)])
    assert rc == 2
    assert "no script" in capsys.readouterr().err


def test_approve_returns_zero(tmp_path, monkeypatch, capsys):
    _install_script(tmp_path, f"#!/usr/bin/env bash\ncat <<'EOF'\n{_MANIFEST_JSON}\nEOF\n")
    _patch_judge(monkeypatch, {
        "verdict": "approve", "summary": "looks good", "issues": [], "blocking": [],
    })
    rc = cli.main(["visual-judge", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "verdict: approve" in out
    assert "looks good" in out


def test_request_changes_returns_one_and_prints_issues(tmp_path, monkeypatch, capsys):
    _install_script(tmp_path, f"#!/usr/bin/env bash\ncat <<'EOF'\n{_MANIFEST_JSON}\nEOF\n")
    _patch_judge(monkeypatch, {
        "verdict": "request_changes", "summary": "broken",
        "issues": [{"severity": "major", "location": "home",
                    "problem": "red error overlay", "fix": "fix the runtime error"}],
        "blocking": [{"severity": "major", "location": "home",
                      "problem": "red error overlay", "fix": "fix the runtime error"}],
    })
    rc = cli.main(["visual-judge", str(tmp_path)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "verdict: request_changes" in out
    assert "red error overlay" in out
    assert "fix the runtime error" in out


def test_json_mode_emits_full_dict(tmp_path, monkeypatch, capsys):
    _install_script(tmp_path, f"#!/usr/bin/env bash\ncat <<'EOF'\n{_MANIFEST_JSON}\nEOF\n")
    _patch_judge(monkeypatch, {
        "verdict": "approve", "summary": "clean", "issues": [], "blocking": [],
    })
    rc = cli.main(["visual-judge", str(tmp_path), "--json"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["verdict"] == "approve" and parsed["summary"] == "clean"


def test_judge_failure_returns_infra_error(tmp_path, monkeypatch, capsys):
    _install_script(tmp_path, f"#!/usr/bin/env bash\ncat <<'EOF'\n{_MANIFEST_JSON}\nEOF\n")

    async def boom(**_kw):
        raise RuntimeError("claude unreachable")
    monkeypatch.setattr("devclaw.quality.visual_judge.judge_screenshots", boom)

    rc = cli.main(["visual-judge", str(tmp_path)])
    assert rc == 2
    assert "visual judge failed" in capsys.readouterr().err


def test_custom_rubric_path_is_read(tmp_path, monkeypatch):
    _install_script(tmp_path, f"#!/usr/bin/env bash\ncat <<'EOF'\n{_MANIFEST_JSON}\nEOF\n")
    rubric = tmp_path / "my-rubric.md"
    rubric.write_text("Show the nav.")
    seen = {}

    async def capture(**kwargs):
        seen.update(kwargs)
        return {"verdict": "approve", "summary": "", "issues": [], "blocking": []}
    monkeypatch.setattr("devclaw.quality.visual_judge.judge_screenshots", capture)

    rc = cli.main(["visual-judge", str(tmp_path), "--rubric", str(rubric)])
    assert rc == 0
    assert "Show the nav." in seen["rubric_per_repo"]


def test_manifest_not_an_object_returns_infra_error(tmp_path, capsys):
    _install_script(tmp_path, "#!/usr/bin/env bash\necho 'not json'\n")
    rc = cli.main(["visual-judge", str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "bad json" in err or "no manifest" in err
