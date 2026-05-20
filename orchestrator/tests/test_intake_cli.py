"""Tests for the new flag-based `devclaw-orchestrator intake` CLI surface.

Exercises the shared `intake_from_prose` entrypoint via the argparse layer:
  - stdin and --prose paths both work
  - stdout is a single parseable JSON line
  - stderr carries human-readable progress narration
  - idempotency: byte-identical (prose, --from) yields state=duplicate AND
    only one spec.yaml on disk
  - exit codes: 0 on success (new and duplicate), non-zero on parse failure
  - the same dedup also fires when called as a shared internal function
    (so the Telegram surface and the CLI agree)
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

from orchestrator import cli
from orchestrator.intake import (
    _intake_hash,
    intake_from_prose,
)
from orchestrator.runners._subprocess import SubprocessResult


def _mock_claude_json(payload: dict):
    return SubprocessResult(
        status="done",
        parsed_json=payload,
        raw_stdout=str(payload),
        raw_stderr="",
        returncode=0,
    )


def _mock_claude_failure():
    return SubprocessResult(
        status="blocked",
        parsed_json=None,
        raw_stdout="",
        raw_stderr="",
        returncode=0,
        blocker="no_parseable_result_json",
    )


# ─── _intake_hash properties ─────────────────────────────────────────────────


def test_intake_hash_is_deterministic_across_processes():
    """SHA-256 must be stable (unlike Python's salted hash())."""
    a = _intake_hash("file a typo", "pc-kit")
    b = _intake_hash("file a typo", "pc-kit")
    assert a == b
    # known length for sha256 hex
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_intake_hash_differs_on_prose_change():
    assert _intake_hash("alpha", "pc-kit") != _intake_hash("alpha ", "pc-kit")
    assert _intake_hash("alpha", "pc-kit") != _intake_hash("alpha", "telegram")


# ─── intake_from_prose: shared internal function ─────────────────────────────


def _stub_claude_payload():
    return _mock_claude_json(
        {
            "kind": "code",
            "target_repo": "dsdevq/devclaw",
            "target_branch": "main",
            "project": None,
            "acceptance_criteria": ["x"],
            "budget_seconds": 1200,
            "notes": "noop",
        }
    )


def test_intake_from_prose_returns_new_then_duplicate(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()

    with mock.patch("orchestrator.intake.run_claude", return_value=_stub_claude_payload()):
        r1 = intake_from_prose("rename foo to bar", from_surface="pc-kit", life_root=life)
        r2 = intake_from_prose("rename foo to bar", from_surface="pc-kit", life_root=life)

    assert r1 is not None and r2 is not None
    assert r1.task_id == r2.task_id
    assert r1.state == "new"
    assert r2.state == "duplicate"
    assert r1.budget_min == 20  # 1200s / 60
    # Exactly one spec.yaml on disk for this dedup key.
    specs = list(life.glob("**/spec.yaml"))
    assert len(specs) == 1, specs


def test_intake_from_prose_distinct_prose_creates_distinct_specs(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()

    with mock.patch("orchestrator.intake.run_claude", return_value=_stub_claude_payload()):
        r1 = intake_from_prose("task A", from_surface="pc-kit", life_root=life)
        r2 = intake_from_prose("task B", from_surface="pc-kit", life_root=life)

    assert r1 is not None and r2 is not None
    assert r1.task_id != r2.task_id
    assert r1.state == "new"
    assert r2.state == "new"
    assert len(list(life.glob("**/spec.yaml"))) == 2


def test_intake_from_prose_distinct_from_surface_creates_distinct_specs(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()

    with mock.patch("orchestrator.intake.run_claude", return_value=_stub_claude_payload()):
        r1 = intake_from_prose("same prose", from_surface="pc-kit", life_root=life)
        r2 = intake_from_prose("same prose", from_surface="telegram", life_root=life)

    assert r1.state == "new"
    assert r2.state == "new"
    assert r1.task_id != r2.task_id


def test_intake_from_prose_returns_none_on_intake_failure(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()
    with mock.patch("orchestrator.intake.run_claude", return_value=_mock_claude_failure()):
        result = intake_from_prose("anything", from_surface="cli", life_root=life)
    assert result is None
    assert list(life.glob("**/spec.yaml")) == []


def test_intake_from_prose_progress_callback_fires(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()
    messages: list[str] = []

    with mock.patch("orchestrator.intake.run_claude", return_value=_stub_claude_payload()):
        intake_from_prose(
            "x", from_surface="cli", life_root=life, progress=lambda m: messages.append(m)
        )
    assert messages, "expected progress callback to be invoked at least once"
    assert any("fingerprint=" in m for m in messages)


def test_intake_from_prose_drops_stale_index_entry(tmp_path: Path):
    """If the indexed spec_path vanishes (operator deleted the dir), the next
    call should NOT report duplicate — it should re-run intake and re-key."""
    life = tmp_path / "life"
    life.mkdir()

    with mock.patch("orchestrator.intake.run_claude", return_value=_stub_claude_payload()):
        r1 = intake_from_prose("ghosty", from_surface="cli", life_root=life)
        assert r1 is not None
        # Nuke the on-disk task dir to simulate operator-side deletion.
        r1.spec_path.parent.unlink() if r1.spec_path.is_symlink() else None
        # Remove the whole task directory
        for p in r1.spec_path.parent.iterdir():
            p.unlink()
        r1.spec_path.parent.rmdir()

        r2 = intake_from_prose("ghosty", from_surface="cli", life_root=life)
    assert r2 is not None
    assert r2.state == "new"


# ─── CLI surface ─────────────────────────────────────────────────────────────


def _run_cli(argv: list[str], stdin_text: str = "") -> tuple[int, str, str]:
    """Run cli.main() with argv injected. Capture stdout/stderr."""
    old_argv = sys.argv
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.argv = ["devclaw-orchestrator"] + argv
    sys.stdin = io.StringIO(stdin_text)
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    try:
        rc = cli.main()
        return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
    finally:
        sys.argv = old_argv
        sys.stdin = old_stdin
        sys.stdout = old_stdout
        sys.stderr = old_stderr


def test_cli_intake_via_prose_flag(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()
    with mock.patch("orchestrator.intake.run_claude", return_value=_stub_claude_payload()):
        rc, out, err = _run_cli(
            ["intake", "--prose", "fix a typo", "--from", "pc-kit", "--life", str(life)]
        )
    assert rc == 0, err
    # stdout is exactly one JSON line
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload) >= {"task_id", "spec_path", "budget_min", "target_repo", "state"}
    assert payload["state"] == "new"
    # stderr carries progress narration
    assert err.strip(), "expected progress on stderr"


def test_cli_intake_via_stdin(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()
    with mock.patch("orchestrator.intake.run_claude", return_value=_stub_claude_payload()):
        rc, out, err = _run_cli(
            ["intake", "--from", "pc-kit", "--life", str(life)],
            stdin_text="please refactor the foo bar baz module",
        )
    assert rc == 0, err
    payload = json.loads(out.strip())
    assert payload["state"] == "new"


def test_cli_intake_idempotent_returns_duplicate_state(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()
    with mock.patch("orchestrator.intake.run_claude", return_value=_stub_claude_payload()):
        rc1, out1, _ = _run_cli(
            ["intake", "--prose", "same task", "--from", "pc-kit", "--life", str(life)]
        )
        rc2, out2, _ = _run_cli(
            ["intake", "--prose", "same task", "--from", "pc-kit", "--life", str(life)]
        )
    assert rc1 == 0 and rc2 == 0
    a = json.loads(out1.strip())
    b = json.loads(out2.strip())
    assert a["task_id"] == b["task_id"]
    assert a["state"] == "new"
    assert b["state"] == "duplicate"
    specs = list(life.glob("**/spec.yaml"))
    assert len(specs) == 1


def test_cli_intake_empty_input_exits_nonzero(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()
    rc, out, err = _run_cli(["intake", "--from", "pc-kit", "--life", str(life)], stdin_text="")
    assert rc != 0
    assert "empty" in err.lower()


def test_cli_intake_intake_failure_exits_nonzero(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()
    with mock.patch("orchestrator.intake.run_claude", return_value=_mock_claude_failure()):
        rc, out, err = _run_cli(
            ["intake", "--prose", "garbled", "--from", "pc-kit", "--life", str(life)]
        )
    assert rc != 0
    # nothing valid on stdout
    assert out.strip() == "" or "error" in out.lower() or "{" not in out
