"""Container-hygiene tests — the startup orphan sweep + the bounded teardown.

Two leaks these pin closed (T0.5):

1. **Orphaned sandboxes survive a devclaw restart.** ``--rm`` only fires when
   its own ``docker run`` client exits, so a process death mid-task leaves the
   container running forever while crash recovery re-runs the task in a SECOND
   container. Every sandbox now carries the ``devclaw.sandbox=1`` label and
   ``sweep_orphan_sandboxes()`` reaps everything matching it at startup —
   and ONLY that label: deploy containers (``devclaw.deploy=1``) are out of
   scope.
2. **``_teardown`` could hang forever.** Its ``docker rm -f`` wait was
   unbounded, so a wedged docker daemon defeated the task wall-clock timeout
   that teardown exists to enforce. The wait is now bounded by
   ``TEARDOWN_TIMEOUT_S``.

All docker interaction goes through patched seams (``_docker_run_sync`` /
``asyncio.create_subprocess_exec``) — no docker needed, mirroring how
``test_deploy.py`` patches ``deploy._run``.
"""

import asyncio
import subprocess

from devclaw.engine import sandcastle as sc


def _completed(args, rc=0, stdout=""):
    return subprocess.CompletedProcess(
        args=args, returncode=rc, stdout=stdout, stderr=""
    )


# ---- sweep_orphan_sandboxes ----


def test_sweep_reaps_each_labeled_container(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(args)
        if args[0] == "ps":
            return _completed(args, stdout="aaa111\nbbb222\n")
        return _completed(args)

    monkeypatch.setattr(sc, "_docker_run_sync", fake_run)
    assert sc.sweep_orphan_sandboxes() == 2
    # ps filters on the sandbox label ALONE — the name is never persisted, and
    # a name-based sweep would miss every leaked container anyway
    assert calls[0] == ["ps", "-q", "--filter", f"label={sc.SANDBOX_LABEL}"]
    # one rm -f per returned id
    assert calls[1:] == [["rm", "-f", "aaa111"], ["rm", "-f", "bbb222"]]


def test_sweep_filter_is_the_sandbox_label_only(monkeypatch):
    # Deploy containers (devclaw.deploy=1, e.g. a live devclaw-deploy-<slug>)
    # must never match the sweep — a startup would take the deployment down.
    seen: dict[str, list[str]] = {}

    def fake_run(args):
        if args[0] == "ps":
            seen["ps"] = args
        return _completed(args, stdout="")

    monkeypatch.setattr(sc, "_docker_run_sync", fake_run)
    sc.sweep_orphan_sandboxes()
    assert seen["ps"].count("--filter") == 1
    assert "label=devclaw.sandbox=1" in seen["ps"]
    assert "devclaw.deploy" not in " ".join(seen["ps"])


def test_sweep_returns_zero_when_docker_missing(monkeypatch):
    # Host/stub engine environments and CI have no docker — silent no-op.
    def fake_run(args):
        raise FileNotFoundError("No such file or directory: 'docker'")

    monkeypatch.setattr(sc, "_docker_run_sync", fake_run)
    assert sc.sweep_orphan_sandboxes() == 0


def test_sweep_returns_zero_when_ps_fails(monkeypatch):
    monkeypatch.setattr(
        sc, "_docker_run_sync", lambda args: _completed(args, rc=1)
    )
    assert sc.sweep_orphan_sandboxes() == 0


def test_sweep_never_raises_on_timeout(monkeypatch):
    # A wedged daemon makes the CLI hang until subprocess.run's timeout — the
    # sweep must swallow it (startup proceeds; the leak waits for a healthier day).
    def fake_run(args):
        raise subprocess.TimeoutExpired(cmd=["docker", *args], timeout=10)

    monkeypatch.setattr(sc, "_docker_run_sync", fake_run)
    assert sc.sweep_orphan_sandboxes() == 0


def test_sweep_survives_one_failed_rm(monkeypatch):
    # A single unremovable container doesn't abort the sweep or inflate the count.
    def fake_run(args):
        if args[0] == "ps":
            return _completed(args, stdout="dead1\ndead2\ndead3\n")
        if args[-1] == "dead2":
            raise subprocess.TimeoutExpired(cmd=["docker", *args], timeout=10)
        return _completed(args)

    monkeypatch.setattr(sc, "_docker_run_sync", fake_run)
    assert sc.sweep_orphan_sandboxes() == 2


# ---- owner-scoped sweep (multi-process seam) ----
#
# Two devclaw processes legitimately share one docker daemon (the live service
# + a one-off measure/eval run). "Every sandbox-labeled container is orphaned
# at MY startup" only holds per-instance: the unscoped sweep was live friendly
# fire — a service restart mid-eval SIGKILLed the eval's in-flight sandboxes
# (exit 137, 2026-07-21).


def test_sweep_scoped_to_owner_spares_other_instances_sandboxes(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(args)
        if args[0] == "ps":
            # own, a concurrent instance's, and a legacy pre-scoping container
            return _completed(args, stdout="own1 abc123\nforeign1 def456\nlegacy1 \n")
        return _completed(args)

    monkeypatch.setattr(sc, "_docker_run_sync", fake_run)
    assert sc.sweep_orphan_sandboxes("abc123") == 2
    rms = [a for a in calls if a[0] == "rm"]
    # own id reaped, legacy (no owner label) reaped, the OTHER instance's spared
    assert rms == [["rm", "-f", "own1"], ["rm", "-f", "legacy1"]]


def test_sweep_owner_query_keeps_the_sandbox_label_filter(monkeypatch):
    # Scoping must narrow the sweep, never widen it: the ps query still filters
    # on the sandbox label (deploy containers stay out of scope) and asks for
    # the owner label per container.
    seen: dict[str, list[str]] = {}

    def fake_run(args):
        if args[0] == "ps":
            seen["ps"] = args
        return _completed(args, stdout="")

    monkeypatch.setattr(sc, "_docker_run_sync", fake_run)
    sc.sweep_orphan_sandboxes("abc123")
    assert f"label={sc.SANDBOX_LABEL}" in seen["ps"]
    assert sc.SANDBOX_OWNER_LABEL_KEY in " ".join(seen["ps"])
    assert "devclaw.deploy" not in " ".join(seen["ps"])


def test_sandbox_launch_stamps_owner_label():
    args = sc._build_docker_args(
        container_name="devclaw-deadbeef",
        host_bind_path="/host/ws",
        claude_dir="/home/u/.claude",
        payload="{}",
        owner_id="abc123",
    )
    i = args.index(f"{sc.SANDBOX_OWNER_LABEL_KEY}=abc123")
    assert args[i - 1] == "--label"


def test_sandbox_launch_without_owner_omits_owner_label():
    # None (tests / direct callers) → argv byte-identical to the pre-scoping
    # posture: no dangling --label, no empty owner value.
    args = sc._build_docker_args(
        container_name="devclaw-deadbeef",
        host_bind_path="/host/ws",
        claude_dir="/home/u/.claude",
        payload="{}",
    )
    assert not any(a.startswith(sc.SANDBOX_OWNER_LABEL_KEY) for a in args)


def test_sandbox_owner_id_is_stable_and_instance_specific():
    # Same DB path → same id across restarts (own orphans stay reapable);
    # different DB path (live devclaw.db vs a measure.db) → different id.
    assert sc.sandbox_owner_id("/var/lib/devclaw/devclaw.db") == sc.sandbox_owner_id(
        "/var/lib/devclaw/devclaw.db"
    )
    assert sc.sandbox_owner_id("/var/lib/devclaw/devclaw.db") != sc.sandbox_owner_id(
        "/var/lib/devclaw/workspaces/.measure/measure.db"
    )


# ---- bounded _teardown ----


class _DeadProc:
    """The docker-run client is already gone — exercises the tolerated
    ProcessLookupError branch of proc.kill()."""

    def kill(self):
        raise ProcessLookupError


class _WedgedKiller:
    """A `docker rm -f` whose wait() never completes — the wedged-daemon case."""

    async def wait(self):
        await asyncio.Event().wait()  # never set — only wait_for's cancel ends it


async def test_teardown_returns_when_docker_rm_wedges(monkeypatch, capsys):
    # The unbounded `await killer.wait()` used to hang here forever, defeating
    # the task wall-clock timeout (asyncio.wait_for waits for the cancelled
    # coroutine's cleanup — i.e. _teardown — before raising).
    async def fake_exec(*args, **kwargs):
        return _WedgedKiller()

    monkeypatch.setattr(sc.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sc, "TEARDOWN_TIMEOUT_S", 0.05)
    # Outer guard: if the reaper wait ever becomes unbounded again, this fails
    # fast instead of hanging the suite.
    await asyncio.wait_for(sc._teardown(_DeadProc(), "devclaw-wedged"), timeout=2)
    err = capsys.readouterr().err
    assert "devclaw-wedged" in err
    assert "timed out" in err


async def test_teardown_quiet_on_prompt_reap(monkeypatch, capsys):
    # Healthy daemon: rm -f returns promptly, no timeout noise.
    class PromptKiller:
        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        return PromptKiller()

    monkeypatch.setattr(sc.asyncio, "create_subprocess_exec", fake_exec)
    await asyncio.wait_for(sc._teardown(_DeadProc(), "devclaw-clean"), timeout=2)
    assert "timed out" not in capsys.readouterr().err
