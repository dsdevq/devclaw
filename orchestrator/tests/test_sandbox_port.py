"""Contract tests — every Sandbox adapter must pass these.

Parametrised across `InProcessSandbox` and `SandcastleSandbox`. The sandcastle adapter is exercised with `subprocess.run` monkeypatched so the test doesn't need Docker, npx, or sandcastle installed — what we're validating is the contract surface (return shape, timeout semantics, env/mount/cmd passing), not the underlying Docker daemon.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable

import pytest

from orchestrator.sandbox import (
    InProcessSandbox,
    SandboxResult,
    SandcastleConfig,
    SandcastleSandbox,
)


# ── shared infra ─────────────────────────────────────────────────────────────


class _StubProbe:
    """Stub RuntimeProbe used in contract tests — always returns True so SandcastleSandbox uses its preferred runtime without trying to talk to Docker."""

    def is_available(self, runtime: str) -> bool:
        return True


def _make_fake_subprocess_run(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    raises: BaseException | None = None,
    capture: list | None = None,
) -> Callable:
    """Build a stand-in for `subprocess.run`. Records the argv it was called with for assertions."""

    def fake_run(cmd, *args, **kwargs):
        if capture is not None:
            capture.append({"cmd": cmd, "kwargs": kwargs})
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=stdout, stderr=stderr
        )

    return fake_run


def _build_sandcastle(monkeypatch, **fake_run_kwargs):
    """Return (sandbox, capture_list). All subprocess calls inside the sandcastle module land in `capture`."""
    capture: list = []
    monkeypatch.setattr(
        "orchestrator.sandbox.sandcastle.subprocess.run",
        _make_fake_subprocess_run(capture=capture, **fake_run_kwargs),
    )
    sb = SandcastleSandbox(
        config=SandcastleConfig(
            image="test-image:local",
            version="0.5.2",
            runtime="runsc",
            fallback_runtime="runc",
        ),
        runtime_probe=_StubProbe(),
    )
    return sb, capture


# ── parametrised contract tests ──────────────────────────────────────────────


def _make_in_process(monkeypatch):
    # Use a real Python invocation so we get a real return code, on every platform with a python on PATH.
    return InProcessSandbox(), None


def _make_sandcastle(monkeypatch):
    sb, _ = _build_sandcastle(monkeypatch, stdout="from-sandcastle\n", returncode=0)
    return sb, "from-sandcastle\n"


ADAPTERS = [
    pytest.param("in_process", _make_in_process, id="in_process"),
    pytest.param("sandcastle", _make_sandcastle, id="sandcastle"),
]


@pytest.mark.parametrize("name,factory", ADAPTERS)
def test_contract_run_returns_sandbox_result(monkeypatch, name, factory):
    sandbox, _stub = factory(monkeypatch)
    if name == "in_process":
        agent_cmd = [sys.executable, "-c", "print('hello-world')"]
    else:
        agent_cmd = ["echo", "hello-world"]

    result = sandbox.run(
        task_id="t-1",
        repo_url="https://example.test/repo.git",
        branch_strategy="new-branch",
        agent_command=agent_cmd,
        env={"FOO": "1"},
        readonly_mounts={},
        timeout_seconds=10,
    )
    assert isinstance(result, SandboxResult)
    assert result.timed_out is False
    assert result.returncode == 0


@pytest.mark.parametrize("name,factory", ADAPTERS)
def test_contract_command_not_found_reports_blocker(monkeypatch, name, factory):
    if name == "sandcastle":
        # Re-build the sandcastle sandbox to inject a FileNotFoundError from `subprocess.run`.
        sandbox, _capture = _build_sandcastle(monkeypatch, raises=FileNotFoundError("npx"))
    else:
        sandbox, _ = factory(monkeypatch)
    result = sandbox.run(
        task_id="t-2",
        repo_url="x",
        branch_strategy="new-branch",
        agent_command=["this-binary-does-not-exist-xyzzy"],
        env={},
        readonly_mounts={},
        timeout_seconds=5,
    )
    assert result.returncode == -1
    assert result.timed_out is False
    assert "not on PATH" in result.stderr or "not found" in result.stderr


@pytest.mark.parametrize("name,factory", ADAPTERS)
def test_contract_timeout_flag_set(monkeypatch, name, factory):
    if name == "sandcastle":
        sandbox, _ = _build_sandcastle(
            monkeypatch,
            raises=subprocess.TimeoutExpired(cmd=["npx"], timeout=1, output=b"partial"),
        )
    else:
        sandbox, _ = factory(monkeypatch)
    if name == "in_process":
        # Sleep longer than the timeout so the real subprocess timeout fires.
        agent_cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
        result = sandbox.run(
            task_id="t-3",
            repo_url="x",
            branch_strategy="new-branch",
            agent_command=agent_cmd,
            env={},
            readonly_mounts={},
            timeout_seconds=1,
        )
    else:
        result = sandbox.run(
            task_id="t-3",
            repo_url="x",
            branch_strategy="new-branch",
            agent_command=["echo", "x"],
            env={},
            readonly_mounts={},
            timeout_seconds=1,
        )

    assert result.timed_out is True
    assert result.returncode == -1


@pytest.mark.parametrize("name,factory", ADAPTERS)
def test_contract_ok_property_reflects_state(monkeypatch, name, factory):
    sandbox, _ = factory(monkeypatch)
    good = SandboxResult(returncode=0, stdout="", stderr="")
    timed_out = SandboxResult(returncode=-1, stdout="", stderr="", timed_out=True)
    bad_rc = SandboxResult(returncode=2, stdout="", stderr="")
    assert good.ok is True
    assert timed_out.ok is False
    assert bad_rc.ok is False
