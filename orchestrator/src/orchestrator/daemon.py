"""Long-running scheduler — replaces the OpenClaw cron entries `task_dispatch_15m` and `curator_30m`.

OpenClaw's cron only fires agent-message payloads (every tick spawns a Claude
subagent), which contradicts the mechanism/cognition split that drove the port.
Running the orchestrator as its own container with an internal loop keeps every
sweep + supervise tick at zero LLM tokens.

The loop interleaves two cadences:
  - sweep: reap + watchdog + dispatch over ~/.life/*/spec.yaml every SWEEP_INTERVAL
  - supervise: tick every active Run dag every SUPERVISE_INTERVAL

Both cadences honor the killswitch at ~/.life/system/cron-paused (sweep already
checks; supervise checks here).

Stagger: supervise is offset by SUPERVISE_OFFSET to avoid contending with sweep
on the first tick after process start.
"""

from __future__ import annotations

import datetime as dt
import logging
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.audits.state_currency import AuditReport, run_and_write as run_state_currency_audit
from orchestrator.supervisor import tick_run
from orchestrator.sweep import is_killswitch_set, sweep_once
from orchestrator.state.models import RequesterRoute

logger = logging.getLogger(__name__)


AnnounceCallback = Callable[[str, str, str], None]
"""(channel, target, message) → None. Mirrors supervisor's AnnounceCallback shape but
also carries the target so the daemon can hand off straight to a per-route delivery
(e.g. `openclaw message send --channel <c> --target <t>`)."""


def _noop_announce(channel: str, target: str, message: str) -> None:  # noqa: ARG001
    logger.info("announce(%s -> %s): %s", channel, target, message)


@dataclass(frozen=True)
class DaemonConfig:
    life_root: Path
    sweep_interval_s: float = 15 * 60
    supervise_interval_s: float = 30 * 60
    supervise_offset_s: float = 60.0
    audit_interval_s: float = 24 * 60 * 60
    audit_offset_s: float = 120.0
    telegram_chat: str = "default"
    announce: AnnounceCallback = field(default=_noop_announce)


SweepFn = Callable[[Path], object]
SuperviseFn = Callable[[Path, RequesterRoute], object]
AuditFn = Callable[[Path], object]


_AUDIT_SUMMARY_CAP = 500


def _build_audit_summary(report: AuditReport, report_path: Path) -> str:
    summary = (
        "⚠️ State-currency drift detected\n"
        f"• Retired-term hits: {len(report.retired_hits)}\n"
        f"• Missing components: {len(report.missing_components)}\n"
        f"Report: {report_path}"
    )
    if len(summary) > _AUDIT_SUMMARY_CAP:
        summary = summary[: _AUDIT_SUMMARY_CAP - 1] + "…"
    return summary


def _default_audit(life_root: Path) -> object:
    report, report_path = run_state_currency_audit(life_root)
    return report, report_path


def _default_sweep(life_root: Path) -> object:
    return sweep_once(life_root)


def _default_supervise_all(life_root: Path, route: RequesterRoute) -> object:
    dags = list(life_root.glob("projects/*/runs/*/dag.yaml"))
    summaries: list[str] = []
    for dag_path in dags:
        try:
            result = tick_run(dag_path, life_root=life_root, requester_route=route)
            summaries.append(result.summary())
        except Exception as exc:  # noqa: BLE001
            summaries.append(f"error in {dag_path}: {exc}")
    return summaries


def run_daemon(
    config: DaemonConfig,
    *,
    shutdown: threading.Event | None = None,
    sweep_fn: SweepFn = _default_sweep,
    supervise_fn: SuperviseFn = _default_supervise_all,
    audit_fn: AuditFn = _default_audit,
) -> None:
    """Run the sweep + supervise loops until `shutdown` is set.

    Both loops run in dedicated threads and use `Event.wait(timeout)` so a SIGTERM
    can cancel the sleep immediately. The function blocks on the threads joining.
    """
    shutdown = shutdown or threading.Event()
    route = RequesterRoute(channel="telegram", to=config.telegram_chat)

    def _sweep_loop() -> None:
        while not shutdown.is_set():
            if is_killswitch_set(config.life_root):
                logger.info("sweep skipped: killswitch present")
            else:
                try:
                    result = sweep_fn(config.life_root)
                    summary = getattr(result, "summary", lambda: str(result))()
                    logger.info("sweep: %s", summary)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("sweep failed: %s", exc)
            if shutdown.wait(config.sweep_interval_s):
                return

    def _supervise_loop() -> None:
        if shutdown.wait(config.supervise_offset_s):
            return
        while not shutdown.is_set():
            if is_killswitch_set(config.life_root):
                logger.info("supervise skipped: killswitch present")
            else:
                try:
                    summaries = supervise_fn(config.life_root, route)
                    if summaries:
                        for s in summaries:
                            logger.info("supervise: %s", s)
                    else:
                        logger.info("supervise: no active runs")
                except Exception as exc:  # noqa: BLE001
                    logger.exception("supervise failed: %s", exc)
            if shutdown.wait(config.supervise_interval_s):
                return

    def _audit_loop() -> None:
        if shutdown.wait(config.audit_offset_s):
            return
        while not shutdown.is_set():
            if is_killswitch_set(config.life_root):
                logger.info("audit skipped: killswitch present")
            else:
                try:
                    today = dt.date.today().isoformat()
                    today_report = (
                        config.life_root / "audits" / f"{today}-state-currency.md"
                    )
                    already_announced_today = today_report.exists()
                    result = audit_fn(config.life_root)
                    report, report_path = result
                    if report.has_drift:
                        logger.warning(
                            "audit: drift detected — %d retired-term hits, %d missing components (%s)",
                            len(report.retired_hits),
                            len(report.missing_components),
                            report_path,
                        )
                        if not already_announced_today:
                            summary = _build_audit_summary(report, report_path)
                            try:
                                config.announce(route.channel, route.to, summary)
                            except Exception as exc:  # noqa: BLE001
                                logger.exception("audit announce failed: %s", exc)
                    else:
                        logger.info("audit: clean (%s)", report_path)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("audit failed: %s", exc)
            if shutdown.wait(config.audit_interval_s):
                return

    t_sweep = threading.Thread(target=_sweep_loop, name="sweep-loop", daemon=True)
    t_super = threading.Thread(target=_supervise_loop, name="supervise-loop", daemon=True)
    t_audit = threading.Thread(target=_audit_loop, name="audit-loop", daemon=True)
    t_sweep.start()
    t_super.start()
    t_audit.start()

    t_sweep.join()
    t_super.join()
    t_audit.join()


def install_signal_handlers(shutdown: threading.Event) -> None:
    """Wire SIGTERM/SIGINT to set the shutdown event."""

    def _handler(signum, _frame) -> None:
        logger.info("received signal %s, shutting down", signum)
        shutdown.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
