"""Shared test fixtures + hermeticity guards."""

import asyncio
import os

import pytest

from devclaw import task_queue
from devclaw.delivery import deploy as _deploy_mod
from devclaw.engine import sandcastle as _sandcastle_mod


@pytest.fixture(autouse=True)
def _disable_review_gate_by_default(monkeypatch):
    """The pre-PR review gate's default reviewer shells out to the real `claude`
    CLI. On a developer machine that's authenticated, an un-injected TaskQueue in
    a test with a real git workspace would make a live, non-deterministic Claude
    call (and in CI it would just fail open). Keep the whole suite hermetic by
    defaulting the gate OFF; the review-gate tests re-enable it explicitly and
    inject a stub reviewer.
    """
    monkeypatch.setattr(task_queue, "REVIEW_GATE_ENABLED", False)


@pytest.fixture(autouse=True)
def _disable_sandbox_sweep_by_default(monkeypatch):
    """``TaskQueue.recover()`` sweeps orphaned sandbox containers via the real
    docker CLI. A test process is NOT the devclaw process: on a docker-enabled
    dev machine a live devclaw could be mid-task, and a test calling recover()
    must never ``docker rm -f`` its containers (the "any labeled container is
    orphaned" premise only holds for the real server's startup). Default the
    sweep to a no-op; the wiring test injects its own recording stub the same
    way, and the sweep's own unit tests patch its subprocess seam directly.
    """
    monkeypatch.setattr(task_queue, "sweep_orphan_sandboxes", lambda: 0)


#: Program basenames a test process must NEVER spawn for real. Built from the
#: same env-derived module constants production uses, so an exotic
#: ``DEVCLAW_DOCKER_BIN`` on a dev host is still caught.
_CONTAINER_BINARIES = frozenset(
    os.path.basename(b)
    for b in (
        "docker",
        "tailscale",
        _deploy_mod.DOCKER_BIN,
        _deploy_mod.TAILSCALE_BIN,
        _sandcastle_mod.DOCKER_BIN,
    )
)

_GUARD_HINT = (
    "The pytest suite is fully stubbed — a test must NEVER launch real "
    "docker/tailscale (a 2026-07-14 pytest run leaked a live, "
    "restart-unless-stopped `devclaw-deploy-g` container on two hosts). "
    "Stub the chokepoint your test actually reaches instead: monkeypatch "
    "`devclaw.delivery.deploy._run` (or `deploy_project` where imported), "
    "patch `devclaw.engine.sandcastle._docker_run_sync`, or disable the "
    "feature (e.g. pass `autodeploy=False`) when the test's intent doesn't "
    "cover deploys."
)


@pytest.fixture(autouse=True)
def _block_real_docker(monkeypatch):
    """Suite-wide hermeticity guard: any attempt to spawn a real ``docker`` or
    ``tailscale`` subprocess fails the test LOUDLY.

    The guard sits at the process-spawn chokepoint (``asyncio.create_subprocess_exec``
    plus sandcastle's sync ``_docker_run_sync`` seam) rather than at each caller, so a
    NEW escape hatch is caught too. It raises via ``pytest.fail`` — a BaseException —
    on purpose: best-effort paths like ``tick_donegate._auto_deploy`` swallow
    ``Exception`` by design, which is exactly how the deploy leak stayed silent.

    Tests that inject fake runners (``deploy._run``, ``_docker_run_sync``) replace
    the guarded seam and never reach this wrapper; pure helpers (``deploy_name``,
    ``_build_run_args``, ``deploy_port``) spawn nothing and stay testable; every
    other subprocess (git, gh, python) passes through untouched.
    """
    real_exec = asyncio.create_subprocess_exec

    async def guarded_exec(program, *args, **kwargs):
        if os.path.basename(str(program)) in _CONTAINER_BINARIES:
            pytest.fail(
                f"BLOCKED: test tried to spawn a real container-daemon subprocess: "
                f"{program} {' '.join(str(a) for a in args[:8])} ...\n{_GUARD_HINT}"
            )
        return await real_exec(program, *args, **kwargs)

    def guarded_docker_sync(args):
        pytest.fail(
            f"BLOCKED: test tried to run real `docker {' '.join(str(a) for a in args[:8])}` "
            f"via sandcastle._docker_run_sync.\n{_GUARD_HINT}"
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded_exec)
    monkeypatch.setattr(_sandcastle_mod, "_docker_run_sync", guarded_docker_sync)
