"""Unit tests for the Codex CLI agent-backend in `_subprocess.py`.

Covers:
  - `_parse_codex_event_stream` event-typing + counts + final-message capture
  - `select_agent_backend` env-var resolution + default
  - `run_codex` happy path / failure paths via monkey-patched `subprocess.run`
  - `run_agent` dispatches to the right backend
  - `code_task._run_codex_in_sandbox` integrates with the Sandbox protocol

No real `codex` binary is invoked; everything is patched.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from orchestrator.runners import _subprocess
from orchestrator.runners._subprocess import (
    DEFAULT_AGENT_BACKEND,
    DEFAULT_CODEX_MODEL,
    SubprocessResult,
    _parse_codex_event_stream,
    run_agent,
    run_claude,
    run_codex,
    select_agent_backend,
)

# ─── _parse_codex_event_stream ──────────────────────────────────────────────


def _event(**kw) -> str:
    return json.dumps(kw)


def test_parse_codex_event_stream_extracts_final_agent_message():
    stream = "\n".join(
        [
            _event(type="thread.started", thread_id="t-1"),
            _event(type="turn.started"),
            _event(
                type="item.completed",
                item={"item_type": "file_change", "path": "README.md"},
            ),
            _event(
                type="item.completed",
                item={
                    "item_type": "command_execution",
                    "command": ["pytest", "-q"],
                },
            ),
            _event(
                type="item.completed",
                item={"item_type": "agent_message", "text": "intermediate"},
            ),
            _event(
                type="item.completed",
                item={
                    "item_type": "agent_message",
                    "text": 'done.\n{"status": "done", "pr_url": "https://x"}',
                },
            ),
            _event(type="turn.completed"),
        ]
    )
    final, counts = _parse_codex_event_stream(stream)
    assert final is not None
    # Final agent_message wins over earlier ones.
    assert "status" in final and "pr_url" in final
    assert counts["thread.started"] == 1
    assert counts["item.completed"] == 4
    assert counts["file_change"] == 1
    assert counts["command_execution"] == 1
    assert counts["agent_message"] == 2


def test_parse_codex_event_stream_tolerates_garbage_lines():
    stream = "\n".join(
        [
            "not json at all",
            _event(type="thread.started", thread_id="t-1"),
            "{another non-json brace}",
            _event(
                type="item.completed",
                item={"item_type": "agent_message", "text": '{"status": "done"}'},
            ),
        ]
    )
    final, counts = _parse_codex_event_stream(stream)
    assert final == '{"status": "done"}'
    assert counts["agent_message"] == 1


def test_parse_codex_event_stream_no_agent_message():
    stream = "\n".join(
        [
            _event(type="thread.started"),
            _event(type="turn.failed", error={"code": "rate_limited"}),
        ]
    )
    final, counts = _parse_codex_event_stream(stream)
    assert final is None
    assert counts["turn.failed"] == 1


def test_parse_codex_event_stream_empty_input():
    final, counts = _parse_codex_event_stream("")
    assert final is None
    assert counts["thread.started"] == 0


# ─── select_agent_backend ───────────────────────────────────────────────────


def test_select_agent_backend_defaults_to_claude(monkeypatch):
    monkeypatch.delenv("DEVCLAW_AGENT_BACKEND", raising=False)
    assert DEFAULT_AGENT_BACKEND == "claude"
    assert select_agent_backend() == "claude"


def test_select_agent_backend_reads_env(monkeypatch):
    monkeypatch.setenv("DEVCLAW_AGENT_BACKEND", "codex")
    assert select_agent_backend() == "codex"


def test_select_agent_backend_case_insensitive(monkeypatch):
    monkeypatch.setenv("DEVCLAW_AGENT_BACKEND", "Codex")
    assert select_agent_backend() == "codex"


def test_select_agent_backend_unknown_falls_back(monkeypatch, caplog):
    monkeypatch.setenv("DEVCLAW_AGENT_BACKEND", "gpt4all")
    assert select_agent_backend(default="claude") == "claude"


def test_select_agent_backend_caller_default_overrides_constant(monkeypatch):
    monkeypatch.delenv("DEVCLAW_AGENT_BACKEND", raising=False)
    assert select_agent_backend(default="codex") == "codex"


# ─── run_codex (happy + failure paths) ──────────────────────────────────────


@dataclass
class _FakeCompleted:
    stdout: str
    stderr: str = ""
    returncode: int = 0


def _make_stream(final_json: str | None) -> str:
    events = [_event(type="thread.started", thread_id="t-1"), _event(type="turn.started")]
    if final_json is not None:
        events.append(
            _event(
                type="item.completed",
                item={"item_type": "agent_message", "text": final_json},
            )
        )
    events.append(_event(type="turn.completed"))
    return "\n".join(events)


def test_run_codex_happy_path(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompleted(
            stdout=_make_stream('{"status": "done", "pr_url": "https://github.com/o/r/pull/1"}'),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_codex(
        "do the thing",
        timeout_seconds=600,
        workdir="/tmp/work",
    )
    assert out.ok is True
    assert out.backend == "codex"
    assert out.parsed_json == {
        "status": "done",
        "pr_url": "https://github.com/o/r/pull/1",
    }
    # Command shape: `codex exec --json --sandbox workspace-write --model <pin>
    # --cd /tmp/work <prompt>`
    assert captured["cmd"][:7] == [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--model",
        DEFAULT_CODEX_MODEL,
    ]
    assert "--cd" in captured["cmd"]
    assert captured["cmd"][-1] == "do the thing"


def test_run_codex_model_env_var_override(monkeypatch):
    monkeypatch.setenv("DEVCLAW_CODEX_MODEL", "gpt-6")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(stdout=_make_stream('{"status": "done"}'))

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_codex("hi", timeout_seconds=10)
    assert out.ok
    # --model arg value lives right after the literal "--model"
    model_idx = captured["cmd"].index("--model")
    assert captured["cmd"][model_idx + 1] == "gpt-6"


def test_run_codex_explicit_model_arg_beats_env(monkeypatch):
    monkeypatch.setenv("DEVCLAW_CODEX_MODEL", "gpt-6")

    def fake_run(cmd, **kwargs):
        # Verify the explicit arg wins; cheap shortcut.
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "gpt-7"
        return _FakeCompleted(stdout=_make_stream('{"status": "done"}'))

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_codex("hi", timeout_seconds=10, model="gpt-7")
    assert out.ok


def test_run_codex_cli_not_found(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("codex")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_codex("hi", timeout_seconds=10)
    assert not out.ok
    assert out.blocker == "codex_cli_not_found"
    assert out.backend == "codex"


def test_run_codex_timeout(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=10, output=b"partial\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_codex("hi", timeout_seconds=10)
    assert not out.ok
    assert out.timed_out is True
    assert out.blocker == "time_budget_exceeded"
    assert out.backend == "codex"


def test_run_codex_nonzero_exit(monkeypatch):
    def fake_run(*args, **kwargs):
        return _FakeCompleted(stdout="", stderr="boom", returncode=42)

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_codex("hi", timeout_seconds=10)
    assert not out.ok
    assert out.blocker == "codex_cli_exit_42"


def test_run_codex_no_final_agent_message(monkeypatch):
    def fake_run(*args, **kwargs):
        # turn.failed only, no agent_message
        stream = "\n".join(
            [
                _event(type="thread.started"),
                _event(type="turn.failed", error={"code": "rate_limited"}),
            ]
        )
        return _FakeCompleted(stdout=stream)

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_codex("hi", timeout_seconds=10)
    assert not out.ok
    assert out.blocker == "codex_no_final_agent_message"


def test_run_codex_unparseable_final_message(monkeypatch):
    def fake_run(*args, **kwargs):
        # agent_message text has no JSON line at all
        return _FakeCompleted(stdout=_make_stream("just narrative prose, no JSON"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_codex("hi", timeout_seconds=10)
    assert not out.ok
    assert out.blocker == "no_parseable_result_json"


def test_run_codex_blocked_status_propagates(monkeypatch):
    def fake_run(*args, **kwargs):
        return _FakeCompleted(
            stdout=_make_stream(
                '{"status": "blocked", "blocker": "acceptance_criteria_unmet"}'
            )
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_codex("hi", timeout_seconds=10)
    assert not out.ok  # ok requires status=="done"
    assert out.blocker == "acceptance_criteria_unmet"
    assert out.parsed_json is not None and out.parsed_json["status"] == "blocked"


# ─── run_agent dispatch ─────────────────────────────────────────────────────


def test_run_agent_dispatches_to_codex(monkeypatch):
    called = {}

    def fake_run_codex(prompt, **kw):
        called["codex"] = (prompt, kw)
        return SubprocessResult(
            status="done",
            parsed_json={"status": "done"},
            raw_stdout="",
            raw_stderr="",
            returncode=0,
            backend="codex",
        )

    monkeypatch.setattr(_subprocess, "run_codex", fake_run_codex)
    out = run_agent("p", timeout_seconds=60, backend="codex")
    assert out.backend == "codex"
    assert called["codex"][0] == "p"


def test_run_agent_dispatches_to_claude(monkeypatch):
    called = {}

    def fake_run_claude(prompt, **kw):
        called["claude"] = (prompt, kw)
        return SubprocessResult(
            status="done",
            parsed_json={"status": "done"},
            raw_stdout="",
            raw_stderr="",
            returncode=0,
            backend="claude",
        )

    monkeypatch.setattr(_subprocess, "run_claude", fake_run_claude)
    out = run_agent("p", timeout_seconds=60, backend="claude")
    assert out.backend == "claude"
    assert called["claude"][0] == "p"


def test_run_agent_no_backend_arg_reads_env(monkeypatch):
    monkeypatch.setenv("DEVCLAW_AGENT_BACKEND", "codex")

    def fake_run_codex(prompt, **kw):
        return SubprocessResult(
            status="done",
            parsed_json={"status": "done"},
            raw_stdout="",
            raw_stderr="",
            returncode=0,
            backend="codex",
        )

    monkeypatch.setattr(_subprocess, "run_codex", fake_run_codex)
    out = run_agent("p", timeout_seconds=60)
    assert out.backend == "codex"


# ─── code_task in-sandbox codex helper ──────────────────────────────────────


def test_run_codex_in_sandbox_happy_path(monkeypatch):
    """The in-sandbox codex helper parses JSONL the same way as run_codex."""
    from orchestrator.runners.code_task import _run_codex_in_sandbox
    from orchestrator.sandbox import CompletedProcess

    class FakeSandbox:
        def __init__(self):
            self.calls = []

        def run(self, cmd, **kw):
            self.calls.append((cmd, kw))
            return CompletedProcess(
                stdout=_make_stream(
                    '{"status": "done", "pr_url": "https://github.com/o/r/pull/9"}'
                ),
                stderr="",
                returncode=0,
                timed_out=False,
            )

        def teardown(self):
            pass

    sb = FakeSandbox()
    out = _run_codex_in_sandbox(
        "do it",
        sandbox=sb,
        timeout_seconds=300,
    )
    assert out.ok
    assert out.backend == "codex"
    # Command lands as `codex exec --json --sandbox workspace-write --model <pin> "do it"`
    cmd = sb.calls[0][0]
    assert cmd[0] == "codex" and cmd[1] == "exec"
    assert "--json" in cmd and "--model" in cmd


def test_run_codex_in_sandbox_handles_not_found(monkeypatch):
    from orchestrator.runners.code_task import _run_codex_in_sandbox
    from orchestrator.sandbox import CompletedProcess

    class FakeSandbox:
        def run(self, cmd, **kw):
            return CompletedProcess(
                stdout="",
                stderr="codex: not found",
                returncode=127,
                timed_out=False,
            )

        def teardown(self):
            pass

    out = _run_codex_in_sandbox("x", sandbox=FakeSandbox(), timeout_seconds=10)
    assert not out.ok
    assert out.blocker == "codex_cli_not_found"


def test_run_codex_in_sandbox_timeout(monkeypatch):
    from orchestrator.runners.code_task import _run_codex_in_sandbox
    from orchestrator.sandbox import CompletedProcess

    class FakeSandbox:
        def run(self, cmd, **kw):
            return CompletedProcess(
                stdout="",
                stderr="",
                returncode=-1,
                timed_out=True,
            )

        def teardown(self):
            pass

    out = _run_codex_in_sandbox("x", sandbox=FakeSandbox(), timeout_seconds=10)
    assert not out.ok
    assert out.timed_out
    assert out.blocker == "time_budget_exceeded"


# ─── run_claude unchanged (regression guard) ────────────────────────────────


def test_run_claude_still_works_after_refactor(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeCompleted(stdout='ok\n{"status": "done", "pr_url": null}\n')

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_claude("hi", timeout_seconds=10)
    assert out.ok
    assert out.backend == "claude"
    assert out.parsed_json == {"status": "done", "pr_url": None}
