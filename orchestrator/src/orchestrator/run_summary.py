"""Per-task run summary writer + reader.

Emits one JSON line per completed task to `~/.life/state/devclaw/runs.jsonl`
so consumers (lifekit-dashboard, ad-hoc `tail`-ers, ops alerts) can ingest
devclaw activity without parsing free-form `result_summary` strings.

Append-only, denormalized, additive. Does NOT replace `result.json`.

Schema (one object per line):

    {
      "ts": "<ISO8601 UTC>",
      "task_id": "<task_id>",
      "kind": "code" | "research" | "propose_change" | "intake" | ...,
      "target_repo": "<owner/repo or null>",
      "status": "done" | "failed" | "watchdog_killed",
      "duration_seconds": <int>,
      "retries": <int>,
      "verifier_result": "passed" | "failed" | "skipped" | null,
      "pr_url": "<github pr url or null>",
      "tokens_total": <int or null>,
      "cost_usd": <float or null>
    }

Concurrent appends are safe: the file is opened with O_APPEND in binary mode
and each summary is written in a single `write()` of <PIPE_BUF bytes, so
POSIX guarantees the kernel will not interleave two writers' lines.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

from orchestrator.state.models import Result, TaskSpec

RunStatus = Literal["done", "failed", "watchdog_killed"]
VerifierResult = Literal["passed", "failed", "skipped"]

DEFAULT_RUNS_PATH = Path("~/.life/state/devclaw/runs.jsonl")


def default_runs_path() -> Path:
    """Resolve the canonical JSONL location. Centralised so tests can override."""
    return DEFAULT_RUNS_PATH.expanduser()


@dataclass(frozen=True)
class RunSummary:
    """One row in `runs.jsonl`. Frozen so accidental mutation can't poison a write."""

    ts: datetime
    task_id: str
    kind: str
    target_repo: str | None
    status: RunStatus
    duration_seconds: int
    retries: int
    verifier_result: VerifierResult | None
    pr_url: str | None
    tokens_total: int | None = None
    cost_usd: float | None = None

    def to_dict(self) -> dict:
        return {
            "ts": self.ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "task_id": self.task_id,
            "kind": self.kind,
            "target_repo": self.target_repo,
            "status": self.status,
            "duration_seconds": int(self.duration_seconds),
            "retries": int(self.retries),
            "verifier_result": self.verifier_result,
            "pr_url": self.pr_url,
            "tokens_total": self.tokens_total,
            "cost_usd": self.cost_usd,
        }


def append_summary(summary: RunSummary, *, path: Path | None = None) -> Path:
    """Atomically append one JSON line to the runs JSONL file.

    Parent directories are created if missing. The file is opened in
    append-binary mode (`"ab"`), and the JSON line + newline is written in a
    single `write()` so concurrent writers cannot interleave bytes (POSIX
    O_APPEND atomicity for sub-PIPE_BUF writes).

    Returns the path written to.
    """
    target = path or default_runs_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(summary.to_dict(), separators=(",", ":")).encode("utf-8") + b"\n"
    with open(target, "ab") as fh:
        fh.write(line)
    return target


def _infer_verifier_result(result: Result | None) -> VerifierResult | None:
    """Best-effort: derive verifier outcome from the runner's `Result`.

    - `done`                        → "passed" (graph never reaches `complete` without passing verify_node)
    - `blocked` + verification_failed → "failed"
    - `blocked` + other blocker     → "skipped" (verifier never got to run, e.g. runner self-blocked)
    - `None`                        → None (watchdog path — no Result at all)
    """
    if result is None:
        return None
    if result.status == "done":
        return "passed"
    if result.blocker == "verification_failed":
        return "failed"
    return "skipped"


def _infer_duration_seconds(spec: TaskSpec, completed_at: datetime | None) -> int:
    """Compute (completed_at - dispatched_at) in whole seconds. 0 if either is missing."""
    end = completed_at or spec.completed_at
    start = spec.dispatched_at
    if end is None or start is None:
        return 0
    delta = (end - start).total_seconds()
    return max(0, int(delta))


def build_summary(
    *,
    spec: TaskSpec,
    result: Result | None,
    status: RunStatus,
    retries: int = 0,
    ts: datetime | None = None,
    tokens_total: int | None = None,
    cost_usd: float | None = None,
    verifier_result: VerifierResult | None | Literal["__infer__"] = "__infer__",
) -> RunSummary:
    """Assemble a RunSummary from a TaskSpec + optional Result + retry count.

    `verifier_result` defaults to inferring from `result`; pass an explicit
    value (including `None`) to override.
    """
    when = ts or datetime.now(timezone.utc)
    vr: VerifierResult | None
    if verifier_result == "__infer__":
        vr = _infer_verifier_result(result)
    else:
        vr = verifier_result  # type: ignore[assignment]

    return RunSummary(
        ts=when,
        task_id=spec.task_id,
        kind=spec.kind.value,
        target_repo=spec.target_repo,
        status=status,
        duration_seconds=_infer_duration_seconds(spec, when),
        retries=retries,
        verifier_result=vr,
        pr_url=(result.pr_url if result is not None else None),
        tokens_total=tokens_total,
        cost_usd=cost_usd,
    )


def record_run(
    *,
    spec: TaskSpec,
    result: Result | None,
    status: RunStatus,
    retries: int = 0,
    tokens_total: int | None = None,
    cost_usd: float | None = None,
    ts: datetime | None = None,
    verifier_result: VerifierResult | None | Literal["__infer__"] = "__infer__",
    path: Path | None = None,
) -> Path:
    """Convenience: build + append in one call. Returns the path written to.

    Best-effort: swallow OSError-class failures so a JSONL write outage cannot
    abort a completed task. Caller can pass a custom `path` for tests.
    """
    summary = build_summary(
        spec=spec,
        result=result,
        status=status,
        retries=retries,
        ts=ts,
        tokens_total=tokens_total,
        cost_usd=cost_usd,
        verifier_result=verifier_result,
    )
    try:
        return append_summary(summary, path=path)
    except OSError:
        return path or default_runs_path()


def read_summaries(
    *,
    path: Path | None = None,
    limit: int | None = None,
    kind: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """Read the JSONL, return the last `limit` rows matching the filters.

    Returns parsed dicts (not RunSummary objects) so callers don't need to
    re-validate fields written by other devclaw versions — JSONL is meant to
    be forward-compatible.
    """
    target = path or default_runs_path()
    if not target.exists():
        return []

    rows: list[dict] = []
    with open(target, "rb") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kind is not None and row.get("kind") != kind:
                continue
            if status is not None and row.get("status") != status:
                continue
            rows.append(row)

    if limit is not None and limit >= 0:
        rows = rows[-limit:]
    return rows


def format_tail(rows: Iterable[dict]) -> str:
    """Pretty-print rows for human-eyed CLI output. Tab-separated, header line."""
    out = [
        "\t".join(
            (
                "ts",
                "task_id",
                "kind",
                "status",
                "verifier",
                "dur_s",
                "retries",
                "pr_url",
            )
        )
    ]
    for r in rows:
        out.append(
            "\t".join(
                str(x)
                for x in (
                    r.get("ts", ""),
                    r.get("task_id", ""),
                    r.get("kind", ""),
                    r.get("status", ""),
                    r.get("verifier_result", ""),
                    r.get("duration_seconds", ""),
                    r.get("retries", ""),
                    r.get("pr_url", "") or "",
                )
            )
        )
    return "\n".join(out)
