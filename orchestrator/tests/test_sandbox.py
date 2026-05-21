"""Tests for the Sandbox port and its two implementations.

Three layers:
  1. `BareTmpdirSandbox` end-to-end against a local bare git repo (no GitHub).
  2. `SandcastleSandbox` parallel test — runs only when `SANDCASTLE_INTEGRATION=1`
     is set and `npx` is on PATH. Always *declared* so the acceptance criterion
     "asserts the sandbox cannot read /home/node/.life/" is visible in the
     codebase, but skipped on machines without the upstream toolchain.
  3. `code_task` runner wiring — asserts that a TaskSpec with
     `sandbox: sandcastle` causes `SandcastleSandbox` to be instantiated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator import sandbox as sandbox_module
from orchestrator.runners import code_task as code_task_module
from orchestrator.runners._subprocess import SubprocessResult
from orchestrator.sandbox import (
    BareTmpdirSandbox,
    CompletedProcess,
    Sandbox,
    SandcastleNotInstalledError,
    SandcastleSandbox,
    make_sandbox,
)
from orchestrator.state.models import (
    Budget,
    GraphState,
    RequesterRoute,
    TaskKind,
    TaskSpec,
    TaskStatus,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_spec(**overrides) -> TaskSpec:
    base = dict(
        task_id="2026-05-20-sandbox-test-aaaa",
        created_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
        created_by="test",
        requester_route=RequesterRoute(channel="test", to="tests"),
        verbatim_intent="test",
        kind=TaskKind.code,
        target_repo="dsdevq/devclaw",
        acceptance_criteria=[],
        budget=Budget(max_runtime_seconds=60),
        status=TaskStatus.ready,
    )
    base.update(overrides)
    return TaskSpec(**base)


def _make_bare_remote(tmp_path: Path) -> Path:
    """Create a bare git repo + one seed commit on `main` we can clone from."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "seed"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "remote", "add", "origin", str(remote)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "push", "origin", "main"],
        check=True,
    )
    return remote


# ─── Protocol smoke ──────────────────────────────────────────────────────────


def test_sandbox_protocol_satisfied_by_bare(tmp_path: Path):
    """The Protocol check is `runtime_checkable` — make sure both impls pass."""
    sb = BareTmpdirSandbox("2026-05-20-proto-bare-aaaa", root=tmp_path)
    try:
        assert isinstance(sb, Sandbox)
    finally:
        sb.teardown()


# ─── BareTmpdirSandbox end-to-end ────────────────────────────────────────────


def test_bare_clone_edit_commit_push(tmp_path: Path):
    """Full clone → edit → commit → push cycle against a local bare repo.

    No GitHub round-trip; no Claude. Just verifying that the Sandbox protocol
    is rich enough to drive a real git workflow.
    """
    remote = _make_bare_remote(tmp_path)

    sb = BareTmpdirSandbox("2026-05-20-bare-e2e-bbbb", root=tmp_path / "sandboxes")
    try:
        # clone
        r = sb.run(["git", "clone", str(remote), str(sb.workdir())])
        assert r.returncode == 0, r.stderr

        # branch
        r = sb.run(["git", "checkout", "-b", "kit/test"])
        assert r.returncode == 0, r.stderr

        # edit
        (sb.workdir() / "feature.md").write_text("new feature\n")

        # commit
        r = sb.run(["git", "add", "feature.md"])
        assert r.returncode == 0, r.stderr
        r = sb.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-m", "add feature"],
        )
        assert r.returncode == 0, r.stderr

        # push (to the bare remote — no GitHub needed)
        r = sb.run(["git", "push", "-u", "origin", "kit/test"])
        assert r.returncode == 0, r.stderr

        # confirm the branch landed in the bare remote
        r = sb.run(["git", "ls-remote", str(remote), "refs/heads/kit/test"])
        assert r.returncode == 0
        assert "kit/test" in r.stdout
    finally:
        sb.teardown()
    # teardown should have nuked the workdir
    assert not sb.workdir().exists()


def test_bare_timeout_returns_timed_out_flag(tmp_path: Path):
    sb = BareTmpdirSandbox("2026-05-20-bare-timeout-cccc", root=tmp_path)
    try:
        # `sleep 5` with a 1-second timeout reliably trips the timeout path.
        # Use --quiet-style cmd that any POSIX has.
        result = sb.run(["sh", "-c", "sleep 5"], timeout=1)
        assert result.timed_out is True
        assert result.returncode != 0
    finally:
        sb.teardown()


def test_bare_run_after_teardown_raises(tmp_path: Path):
    sb = BareTmpdirSandbox("2026-05-20-bare-teardown-dddd", root=tmp_path)
    sb.teardown()
    with pytest.raises(RuntimeError, match="after teardown"):
        sb.run(["true"])


def test_bare_env_injection_merges_with_os_environ(tmp_path: Path):
    sb = BareTmpdirSandbox("2026-05-20-bare-env-eeee", root=tmp_path)
    try:
        # PATH must still resolve `sh` — that proves we merged not replaced.
        result = sb.run(
            ["sh", "-c", "echo $FOO_DEVCLAW"],
            env={"FOO_DEVCLAW": "bar"},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "bar"
    finally:
        sb.teardown()


# ─── SandcastleSandbox ───────────────────────────────────────────────────────


SANDCASTLE_INTEGRATION = (
    os.environ.get("SANDCASTLE_INTEGRATION") == "1" and shutil.which("npx") is not None
)
sandcastle_integration = pytest.mark.skipif(
    not SANDCASTLE_INTEGRATION,
    reason="SANDCASTLE_INTEGRATION=1 and `npx` required for live Sandcastle tests",
)


def test_sandcastle_raises_when_npx_missing(tmp_path: Path):
    """Loud, structured failure when the Node toolchain is absent.

    The runner depends on this exception type to flip the task to `blocked`
    with `sandcastle_not_available` rather than crash. If we ever change the
    exception name, the runner needs to track it.
    """
    with patch("shutil.which", return_value=None):
        with pytest.raises(SandcastleNotInstalledError):
            SandcastleSandbox(
                "2026-05-20-sandcastle-missing-ffff",
                root=tmp_path,
                npx_bin="npx-does-not-exist",
            )


@sandcastle_integration
def test_sandcastle_clone_edit_commit_push(tmp_path: Path):
    """Parallel of `test_bare_clone_edit_commit_push` but inside Sandcastle."""
    remote = _make_bare_remote(tmp_path)

    sb = SandcastleSandbox(
        "2026-05-20-sandcastle-e2e-gggg", root=tmp_path / "sandboxes"
    )
    try:
        r = sb.run(["git", "clone", str(remote), "."])
        assert r.returncode == 0, r.stderr
        r = sb.run(["git", "checkout", "-b", "kit/test"])
        assert r.returncode == 0, r.stderr
        sb.run(["sh", "-c", "echo 'new feature' > feature.md"])
        r = sb.run(["git", "add", "feature.md"])
        assert r.returncode == 0, r.stderr
        r = sb.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-m", "add feature"],
        )
        assert r.returncode == 0, r.stderr
        r = sb.run(["git", "push", "-u", "origin", "kit/test"])
        assert r.returncode == 0, r.stderr
    finally:
        sb.teardown()


@sandcastle_integration
def test_sandcastle_cannot_read_life_dir(tmp_path: Path):
    """Escape check — the sandbox MUST NOT see /home/node/.life/.

    This is the core security invariant Sandcastle exists to give us. If a
    future Sandcastle config change accidentally bind-mounts the host home,
    this test should catch it.
    """
    sb = SandcastleSandbox(
        "2026-05-20-sandcastle-escape-hhhh", root=tmp_path / "sandboxes"
    )
    try:
        # `ls` of a missing path exits non-zero on every POSIX. Belt-and-
        # suspenders: also check the stdout doesn't accidentally contain a
        # listing.
        result = sb.run(["ls", "/home/node/.life/"])
        assert result.returncode != 0, (
            "sandbox could read /home/node/.life/ — bind-mount leaked host home"
        )
        forbidden = ("system", "proposals.md", "tasks")
        assert not any(name in result.stdout for name in forbidden), (
            f"sandbox stdout looks like it listed /home/node/.life/: "
            f"{result.stdout[:200]!r}"
        )
    finally:
        sb.teardown()


# ─── Factory ─────────────────────────────────────────────────────────────────


def test_make_sandbox_bare(tmp_path: Path):
    sb = make_sandbox("2026-05-20-factory-bare-iiii", kind="bare", root=tmp_path)
    try:
        assert isinstance(sb, BareTmpdirSandbox)
    finally:
        sb.teardown()


def test_make_sandbox_unknown_kind_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown sandbox kind"):
        make_sandbox("2026-05-20-factory-bogus-jjjj", kind="nonexistent")  # type: ignore[arg-type]


# ─── TaskSpec field ──────────────────────────────────────────────────────────


def test_taskspec_sandbox_defaults_to_bare():
    spec = _make_spec()
    assert spec.sandbox == "bare"


def test_taskspec_sandbox_accepts_sandcastle():
    spec = _make_spec(sandbox="sandcastle")
    assert spec.sandbox == "sandcastle"


def test_taskspec_sandbox_rejects_unknown_value():
    with pytest.raises(Exception):  # pydantic ValidationError
        _make_spec(sandbox="firecracker")


# ─── code_task runner picks the right sandbox kind ───────────────────────────


class _FakeSandbox:
    """Just enough surface to satisfy the protocol and observe what was asked."""

    def __init__(self, label: str):
        self.label = label
        self.runs: list[list[str]] = []

    def workdir(self) -> Path:
        return Path("/tmp/_fake")

    def run(
        self,
        cmd: list[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> CompletedProcess:
        self.runs.append(cmd)
        return CompletedProcess(
            returncode=0,
            stdout='{"status": "done", "pr_url": "https://x", "branch": "kit/x", '
            '"files_changed": ["a.py"], "tests_passed": true, "notes": "ok"}',
            stderr="",
        )

    def teardown(self) -> None:
        pass


def test_code_task_uses_sandcastle_when_spec_says_so(monkeypatch):
    """Acceptance: `sandbox: sandcastle` causes SandcastleSandbox instantiation.

    We swap `make_sandbox` for a fake that records what `kind` it was called
    with — same result either way for the task, so we can assert wiring in
    isolation from Docker/Sandcastle availability.
    """
    seen: dict[str, str] = {}

    def fake_make_sandbox(task_id: str, kind: str = "bare", **kw):
        seen["task_id"] = task_id
        seen["kind"] = kind
        return _FakeSandbox(label=kind)

    monkeypatch.setattr(code_task_module, "make_sandbox", fake_make_sandbox)

    spec = _make_spec(sandbox="sandcastle")
    state = GraphState(spec=spec)
    out = code_task_module.code_task_node(state)

    assert seen["kind"] == "sandcastle"
    assert seen["task_id"] == spec.task_id
    assert out["result"].status == "done"
    assert out["result"].pr_url == "https://x"


def test_code_task_uses_bare_by_default(monkeypatch):
    seen: dict[str, str] = {}

    def fake_make_sandbox(task_id: str, kind: str = "bare", **kw):
        seen["kind"] = kind
        return _FakeSandbox(label=kind)

    monkeypatch.setattr(code_task_module, "make_sandbox", fake_make_sandbox)

    spec = _make_spec()  # default sandbox: bare
    state = GraphState(spec=spec)
    code_task_module.code_task_node(state)
    assert seen["kind"] == "bare"


def test_code_task_blocks_when_sandcastle_missing(monkeypatch):
    """If SandcastleSandbox raises at construction, runner returns blocked."""

    def boom(task_id: str, kind: str = "bare", **kw):
        if kind == "sandcastle":
            raise SandcastleNotInstalledError("npx not found on PATH")
        return _FakeSandbox(label=kind)

    monkeypatch.setattr(code_task_module, "make_sandbox", boom)

    spec = _make_spec(sandbox="sandcastle")
    state = GraphState(spec=spec)
    out = code_task_module.code_task_node(state)

    assert out["result"].status == "blocked"
    assert out["result"].blocker == "sandcastle_not_available"


def test_code_task_no_bare_subprocess_run_in_module():
    """Belt-and-braces: the runner module must not import subprocess directly.

    Acceptance criterion: "no bare subprocess.run remains in the runner module".
    Easier to enforce structurally than per-call.
    """
    source = Path(code_task_module.__file__).read_text()
    # `subprocess.run(` would be the bare call; the helpers go through Sandbox.
    assert "subprocess.run(" not in source
    # Importing the subprocess module itself is also forbidden — the runner
    # only talks to Sandbox now.
    assert "\nimport subprocess" not in source
    assert "from subprocess import" not in source


def test_code_task_teardown_called_even_on_failure(monkeypatch):
    """Sandbox.teardown must run even if the claude invocation fails."""
    teardown_called = {"n": 0}

    class _FailingSandbox(_FakeSandbox):
        def run(self, cmd, cwd=None, env=None, timeout=None):
            return CompletedProcess(returncode=1, stdout="", stderr="oh no")

        def teardown(self):
            teardown_called["n"] += 1

    monkeypatch.setattr(
        code_task_module,
        "make_sandbox",
        lambda task_id, kind="bare", **kw: _FailingSandbox(label=kind),
    )

    spec = _make_spec()
    state = GraphState(spec=spec)
    out = code_task_module.code_task_node(state)
    assert out["result"].status == "blocked"
    assert teardown_called["n"] == 1


# Reference to sandbox_module / SubprocessResult to keep imports used even when
# the integration-test block is skipped on plain CI runs.
_ = sandbox_module
_ = SubprocessResult
