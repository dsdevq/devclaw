"""_run_visual_verify behavior tests for the in-sandbox runner.

Drives the real bash subprocess via tiny throwaway scripts so we exercise the
actual subprocess + parser path (the seam most likely to break under field
conditions). No docker, no real claude, no real browser — the script just echoes
canned JSON or misbehaves on demand.
"""

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNNER_PATH = _REPO_ROOT / "openhands-runner" / "runner.py"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("oh_runner_visual_under_test", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _install_script(workspace: Path, body: str, *, executable: bool = True) -> Path:
    agent = workspace / ".agent"
    agent.mkdir(exist_ok=True)
    script = agent / "visual-verify.sh"
    script.write_text(body)
    if executable:
        st = script.stat().st_mode
        script.chmod(st | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


# ---------------------------------------------------------------------------


def test_no_script_skips_cleanly(runner, tmp_path):
    ws = _make_workspace(tmp_path)
    out = runner._run_visual_verify(str(ws), "implement_feature", "tid")
    assert out["ran"] is False
    assert out["manifest"] == []
    assert out["reason"] == "no script"


def test_non_executable_script_skips(runner, tmp_path):
    ws = _make_workspace(tmp_path)
    _install_script(ws, "#!/usr/bin/env bash\necho '{}'\n", executable=False)
    out = runner._run_visual_verify(str(ws), "implement_feature", "tid")
    assert out["ran"] is False
    assert "not executable" in out["reason"]


def test_valid_manifest_passthrough(runner, tmp_path):
    ws = _make_workspace(tmp_path)
    manifest = {
        "routes": [
            {"label": "home", "url": "/", "screenshot": "home.png", "console_errors": []},
            {"label": "settings", "url": "/settings", "screenshot": "settings.png"},
        ],
        "notes": "captured 2 routes",
    }
    _install_script(
        ws,
        f"#!/usr/bin/env bash\ncat <<'EOF'\n{json.dumps(manifest)}\nEOF\n",
    )
    out = runner._run_visual_verify(str(ws), "implement_feature", "tid")
    assert out["ran"] is True
    assert out["errors"] == []
    assert len(out["manifest"]) == 2
    assert out["manifest"][0]["label"] == "home"
    assert out["notes"] == "captured 2 routes"
    # evidence dir is created at the well-known sub-path
    assert (ws / ".devclaw-evidence").is_dir()


def test_garbage_stdout_populates_errors(runner, tmp_path):
    ws = _make_workspace(tmp_path)
    _install_script(ws, "#!/usr/bin/env bash\necho 'definitely not json'\n")
    out = runner._run_visual_verify(str(ws), "implement_feature", "tid")
    assert out["ran"] is True
    assert out["manifest"] == []
    assert out["reason"] == "bad json"
    assert out["errors"] and "not JSON" in out["errors"][0]


def test_empty_stdout_is_error(runner, tmp_path):
    ws = _make_workspace(tmp_path)
    _install_script(ws, "#!/usr/bin/env bash\nexit 0\n")
    out = runner._run_visual_verify(str(ws), "implement_feature", "tid")
    assert out["ran"] is True
    assert out["manifest"] == []
    assert "empty" in out["reason"]


def test_non_zero_exit_is_error(runner, tmp_path):
    ws = _make_workspace(tmp_path)
    _install_script(ws, "#!/usr/bin/env bash\necho 'boot failed' >&2\nexit 7\n")
    out = runner._run_visual_verify(str(ws), "implement_feature", "tid")
    assert out["ran"] is True
    assert out["manifest"] == []
    assert "exit 7" in out["reason"]
    assert any("exited 7" in e for e in out["errors"])


def test_timeout_is_skipped_with_partial(runner, tmp_path):
    ws = _make_workspace(tmp_path)
    _install_script(ws, "#!/usr/bin/env bash\necho 'starting'\nsleep 5\n")
    out = runner._run_visual_verify(str(ws), "implement_feature", "tid", timeout=1)
    assert out["ran"] is True
    assert out["manifest"] == []
    assert out["reason"] == "timeout"


def test_script_sees_env_vars(runner, tmp_path):
    ws = _make_workspace(tmp_path)
    # The script echoes the env back into a tiny manifest so we can assert.
    _install_script(
        ws,
        '#!/usr/bin/env bash\n'
        'cat <<EOF\n'
        '{"routes":[{"label":"$DEVCLAW_TASK_KIND",'
        '"url":"port-$DEVCLAW_BROWSER_PORT",'
        '"screenshot":"$DEVCLAW_VISUAL_EVIDENCE_DIR/x.png"}]}\n'
        'EOF\n',
    )
    out = runner._run_visual_verify(str(ws), "fix_bug", "task-42")
    assert out["ran"] is True and out["manifest"]
    first = out["manifest"][0]
    assert first["label"] == "fix_bug"
    assert first["url"].startswith("port-")
    # the script saw the evidence dir env var pointing at the well-known subdir
    assert str(ws / ".devclaw-evidence") in first["screenshot"]


def test_routes_must_be_list_of_dicts(runner, tmp_path):
    ws = _make_workspace(tmp_path)
    # Non-list routes → empty manifest, no errors, marked as no routes.
    _install_script(
        ws,
        '#!/usr/bin/env bash\necho \'{"routes":"oops"}\'\n',
    )
    out = runner._run_visual_verify(str(ws), "implement_feature", "tid")
    assert out["ran"] is True
    assert out["manifest"] == []
    assert out["reason"] == "empty routes"


def test_root_must_be_object(runner, tmp_path):
    ws = _make_workspace(tmp_path)
    _install_script(ws, "#!/usr/bin/env bash\necho '[1,2,3]'\n")
    out = runner._run_visual_verify(str(ws), "implement_feature", "tid")
    assert out["ran"] is True
    assert out["reason"] == "bad shape"
