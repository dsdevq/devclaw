"""Run trace — capture every observable step of a goal as it executes.

The blindness problem: today a refactor can land green tests but quietly break
the *runtime* path — wrong prompt, wrong model, an extra cognition call, a
missing notification — because the suite uses fakes and never watches the real
flow. This module is the safety net. A :class:`Tracer` collects events
fire-and-forget over a contextvar, so any code path (live cognition, tick
handlers, the stub engine) can append a record without threading a parameter
through every call.

Two consumers:
  * the E2E harness (``evals/e2e_trace.py``) — runs a real goal, dumps
    ``trace.json`` + ``timeline.md`` so a human can read what happened.
  * tests — set a tracer for a stub-mode run and assert the expected events
    fired (deliveries grew, deploy fired, the right roles were invoked).

The recorder is **fail-silent**: if no tracer is set, every ``record_*`` is a
no-op. Tracing is opt-in; production stays untouched unless a harness/test
attaches one.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _preview(text: str, n: int = 240) -> str:
    s = (text or "").strip().replace("\n", " ⏎ ")
    return s if len(s) <= n else s[: n - 1] + "…"


@dataclass
class CognitionEvent:
    kind: str = "cognition"
    ts: str = field(default_factory=_now_iso)
    role: str = ""
    model: str = ""
    prompt_hash: str = ""
    prompt_preview: str = ""
    response_preview: str = ""
    #: the FULL response text (T0.5) — plans/evaluations are small, and the
    #: 240-char preview made verdicts unreconstructable (telemetry regexed the
    #: preview and dumped truncations into "unparseable"). The full PROMPT is
    #: deliberately NOT stored in the row — it can exceed 128 KB; goal-scoped
    #: tracers write it to a transcript file instead (see ``transcript_file``).
    response_text: str = ""
    #: transcript filename under ``<goal_dir>/transcripts/`` when the bound
    #: tracer is goal-scoped (PersistentTracer with a goals_dir); "" otherwise.
    transcript_file: str = ""
    latency_ms: int = 0
    error: str = ""
    # Rough proxy for cost: ~4 chars per token. Kept for rows where the CLI
    # envelope was unavailable (legacy rows, raw-stdout fallback). Treat as a
    # relative measure for comparing cascades, not for billing accuracy.
    tokens_in_est: int = 0
    tokens_out_est: int = 0
    #: REAL usage from the ``claude --print --output-format json`` envelope
    #: (T0.5). None → no envelope for this call; fall back to the ``_est``
    #: fields above.
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    cache_read: Optional[int] = None
    cache_creation: Optional[int] = None
    cost_usd: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TickEvent:
    kind: str = "tick"
    ts: str = field(default_factory=_now_iso)
    goal_id: str = ""
    lifecycle: str = ""
    phase: str = ""
    outcome: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DispatchEvent:
    kind: str = "dispatch"
    ts: str = field(default_factory=_now_iso)
    goal_id: str = ""
    tool: str = ""
    ref_id: str = ""
    engine: str = ""           # stub | sandcastle | host | claude_sdk
    is_discovery: bool = False
    is_done_check: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SubprocessEvent:
    """One cross-boundary subprocess invocation — claude --print, docker run,
    git push. Surfaces what would otherwise be invisible in the timeline."""

    kind: str = "subprocess"
    ts: str = field(default_factory=_now_iso)
    cmd: str = ""              # short label: "claude --print", "docker run", "git push"
    argv_head: str = ""        # the first ~120 chars of the actual argv for forensics
    latency_ms: int = 0
    exit_code: Optional[int] = None
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeliveryEvent:
    kind: str = "delivery"
    ts: str = field(default_factory=_now_iso)
    goal_id: str = ""
    action_label: str = ""
    gate_passed: Optional[bool] = None
    pr_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NotifyEvent:
    kind: str = "notify"
    ts: str = field(default_factory=_now_iso)
    level: str = ""
    text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NoteEvent:
    """Free-form event the harness uses to annotate boundaries (start of run,
    swap of subject, etc.). Not emitted by the chef itself."""

    kind: str = "note"
    ts: str = field(default_factory=_now_iso)
    text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TrendCheckEvent:
    """One trend-detector pre-filter pass — fired or not. Emitted every
    heartbeat per (signal, scope) so the first calibration question
    ("why didn't R2 fire last Tuesday?") has an answer in the traces table
    instead of requiring a manual ``git log`` re-run."""

    kind: str = "trend_check"
    ts: str = field(default_factory=_now_iso)
    signal: str = ""           # e.g. "R2"
    scope: str = ""            # "per_project" | "harness_self"
    fired: bool = False
    actual: Optional[float] = None
    threshold: Optional[float] = None
    #: "fired" | "below_threshold" | "cooldown" | "disabled" | "error:<ExcClass>"
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class Tracer:
    """Append-only event recorder. Thread-safe-by-not-being-concurrent: a single
    run drives one tracer through a single asyncio loop. Tracers do NOT mutate
    chef state — they observe it."""

    def __init__(self, label: str = "") -> None:
        self.label = label
        self.started_at = _now_iso()
        self.events: list[dict] = []

    def append(self, event: Any) -> None:
        if hasattr(event, "to_dict"):
            self.events.append(event.to_dict())
        else:
            self.events.append(dict(event))

    def write_transcript(
        self, *, role: str, model: str, prompt: str, response: str,
        tokens_in: Optional[int] = None, tokens_out: Optional[int] = None,
        cost_usd: Optional[float] = None, error: str = "",
    ) -> str:
        """Hook for goal-scoped tracers to persist the FULL prompt + response
        as a transcript file (the full prompt never enters the trace row — it
        can exceed 128 KB). The base in-memory tracer writes nothing and
        returns "" — non-goal-scoped cognition is unchanged."""
        return ""

    # ---- aggregate views ----------------------------------------------------

    def by_kind(self, kind: str) -> list[dict]:
        return [e for e in self.events if e.get("kind") == kind]

    def cognition_total_ms(self) -> int:
        return sum(int(e.get("latency_ms") or 0) for e in self.by_kind("cognition"))

    def cognition_by_role(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.by_kind("cognition"):
            out[e.get("role", "")] = out.get(e.get("role", ""), 0) + 1
        return out

    # ---- persistence --------------------------------------------------------

    def dump_json(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {
                "label": self.label,
                "started_at": self.started_at,
                "ended_at": _now_iso(),
                "events": self.events,
                "summary": {
                    "ticks": len(self.by_kind("tick")),
                    "cognition_calls": len(self.by_kind("cognition")),
                    "cognition_ms": self.cognition_total_ms(),
                    "cognition_by_role": self.cognition_by_role(),
                    "dispatches": len(self.by_kind("dispatch")),
                    "deliveries": len(self.by_kind("delivery")),
                    "notifications": len(self.by_kind("notify")),
                },
            },
            indent=2,
        ))
        return path

    def render_timeline(self) -> str:
        lines = [f"# trace — {self.label or 'unlabeled'}", "", f"_started {self.started_at}_", ""]
        for e in self.events:
            ts = e.get("ts", "")
            kind = e.get("kind", "?")
            if kind == "tick":
                lines.append(f"- `[{ts}]` **tick** `{e.get('goal_id', '')}` lifecycle=`{e.get('lifecycle', '')}` phase=`{e.get('phase', '')}` → `{e.get('outcome', '')}`")
            elif kind == "cognition":
                lat = e.get("latency_ms", 0)
                role = e.get("role", "")
                model = e.get("model", "")
                err = e.get("error", "")
                head = f"- `[{ts}]` **cognition** `{role}` ({model}, {lat}ms)"
                if err:
                    head += f" — ERROR: {err}"
                lines.append(head)
                lines.append(f"    - prompt: `{e.get('prompt_hash', '')}` — _{e.get('prompt_preview', '')}_")
                if not err:
                    lines.append(f"    - response: _{e.get('response_preview', '')}_")
            elif kind == "dispatch":
                tags = []
                if e.get("is_discovery"):
                    tags.append("discovery")
                if e.get("is_done_check"):
                    tags.append("done-check")
                engine = e.get("engine", "")
                if engine:
                    tags.append(f"engine={engine}")
                tag = f" [{', '.join(tags)}]" if tags else ""
                lines.append(f"- `[{ts}]` **dispatch** `{e.get('goal_id', '')}` → `{e.get('tool', '')}` ({e.get('ref_id', '')}){tag}")
            elif kind == "subprocess":
                err = e.get("error", "")
                ec = e.get("exit_code")
                lat = e.get("latency_ms", 0)
                status = "ok" if (err == "" and (ec is None or ec == 0)) else f"FAILED exit={ec} {err[:80]}"
                lines.append(
                    f"- `[{ts}]` **subprocess** `{e.get('cmd', '')}` ({lat}ms, {status})"
                )
                if e.get("argv_head"):
                    lines.append(f"    - argv: `{e['argv_head']}`")
            elif kind == "delivery":
                gp = e.get("gate_passed")
                gate = "✓" if gp is True else ("✗" if gp is False else "—")
                pr = e.get("pr_url", "")
                pr_part = f" [{pr}]" if pr else ""
                lines.append(f"- `[{ts}]` **delivery** `{e.get('goal_id', '')}` gate={gate}{pr_part} — _{e.get('action_label', '')}_")
            elif kind == "notify":
                lines.append(f"- `[{ts}]` **notify** ({e.get('level', '')}) — _{_preview(e.get('text', ''))}_")
            elif kind == "note":
                lines.append(f"- `[{ts}]` **·** _{e.get('text', '')}_")
            elif kind == "trend_check":
                fired = e.get("fired", False)
                sig = e.get("signal", "")
                scope = e.get("scope", "")
                reason = e.get("reason", "")
                actual = e.get("actual")
                threshold = e.get("threshold")
                if fired:
                    head = f"- `[{ts}]` **trend_check** `{sig}` ({scope}) **FIRED** — actual={actual} threshold={threshold}"
                else:
                    head = f"- `[{ts}]` **trend_check** `{sig}` ({scope}) — {reason}"
                lines.append(head)
        return "\n".join(lines) + "\n"

    def dump_timeline(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render_timeline())
        return path


_current: ContextVar[Optional[Tracer]] = ContextVar("devclaw_tracer", default=None)


def set_tracer(t: Optional[Tracer]) -> None:
    _current.set(t)


def get_tracer() -> Optional[Tracer]:
    return _current.get()


class _TracerScope:
    """Context manager that sets the active tracer and properly resets it on
    exit using the ContextVar token, so nested scopes don't leak state."""

    def __init__(self, tracer: Optional[Tracer]) -> None:
        self._tracer = tracer
        self._token: Any = None

    def __enter__(self) -> Optional[Tracer]:
        if self._tracer is not None:
            self._token = _current.set(self._tracer)
        return self._tracer

    def __exit__(self, *_exc: Any) -> None:
        if self._token is not None:
            _current.reset(self._token)
            self._token = None


def tracer_scope(tracer: Optional[Tracer]) -> _TracerScope:
    """Use as ``with tracer_scope(tracer): ...`` to bind a tracer for a block.

    Cleaner than manual set/reset because the ContextVar token is preserved
    even across nested scopes — important when the heartbeat loops over many
    goals and creates a per-goal tracer for each.
    """
    return _TracerScope(tracer)


# ---- recorders (no-ops when no tracer is attached) -------------------------


def record_cognition(
    *, role: str, model: str, prompt: str, response: str = "",
    latency_ms: int = 0, error: str = "",
    tokens_in: Optional[int] = None, tokens_out: Optional[int] = None,
    cache_read: Optional[int] = None, cache_creation: Optional[int] = None,
    cost_usd: Optional[float] = None,
) -> None:
    """Record one cognition call. ``tokens_in``/``tokens_out``/``cache_read``/
    ``cache_creation``/``cost_usd`` carry REAL usage from the CLI's json
    envelope when the caller has it; when absent (stub cognition, raw-stdout
    fallback) the event still carries the legacy len/4 ``_est`` fields."""
    t = _current.get()
    if t is None:
        return
    transcript_file = t.write_transcript(
        role=role, model=model or "(default)", prompt=prompt, response=response,
        tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd, error=error,
    )
    t.append(CognitionEvent(
        role=role, model=model or "(default)",
        prompt_hash=_hash(prompt), prompt_preview=_preview(prompt),
        response_preview=_preview(response),
        response_text=response or "",
        transcript_file=transcript_file,
        latency_ms=latency_ms,
        error=error[:300],
        tokens_in_est=len(prompt or "") // 4,
        tokens_out_est=len(response or "") // 4,
        tokens_in=tokens_in, tokens_out=tokens_out,
        cache_read=cache_read, cache_creation=cache_creation,
        cost_usd=cost_usd,
    ))


def record_tick(*, goal_id: str, lifecycle: str, phase: str, outcome: str) -> None:
    t = _current.get()
    if t is None:
        return
    t.append(TickEvent(goal_id=goal_id, lifecycle=lifecycle, phase=phase, outcome=outcome))


def record_dispatch(
    *, goal_id: str, tool: str, ref_id: str,
    engine: str = "", is_discovery: bool = False, is_done_check: bool = False,
) -> None:
    t = _current.get()
    if t is None:
        return
    t.append(DispatchEvent(
        goal_id=goal_id, tool=tool, ref_id=ref_id, engine=engine,
        is_discovery=is_discovery, is_done_check=is_done_check,
    ))


def record_subprocess(
    *, cmd: str, argv_head: str = "", latency_ms: int = 0,
    exit_code: Optional[int] = None, error: str = "",
) -> None:
    t = _current.get()
    if t is None:
        return
    t.append(SubprocessEvent(
        cmd=cmd, argv_head=argv_head[:120], latency_ms=latency_ms,
        exit_code=exit_code, error=error[:200],
    ))


def record_delivery(*, goal_id: str, action_label: str, gate_passed: Optional[bool], pr_url: str = "") -> None:
    t = _current.get()
    if t is None:
        return
    t.append(DeliveryEvent(
        goal_id=goal_id, action_label=action_label,
        gate_passed=gate_passed, pr_url=pr_url or "",
    ))


def record_notify(*, level: str, text: str) -> None:
    t = _current.get()
    if t is None:
        return
    t.append(NotifyEvent(level=level, text=text))


def record_note(text: str) -> None:
    t = _current.get()
    if t is None:
        return
    t.append(NoteEvent(text=text))


def record_trend_check(
    *,
    signal: str,
    scope: str,
    fired: bool,
    actual: Optional[float] = None,
    threshold: Optional[float] = None,
    reason: str = "",
) -> None:
    """Record one trend-detector pre-filter pass. No-op when no tracer is
    attached (matches the rest of the recorder API)."""
    t = _current.get()
    if t is None:
        return
    t.append(TrendCheckEvent(
        signal=signal, scope=scope, fired=fired,
        actual=actual, threshold=threshold, reason=reason,
    ))


# ---- persistent tracer (production telemetry) ------------------------------


class PersistentTracer(Tracer):
    """A Tracer that ALSO appends every event to a StateStore.

    The base ``Tracer`` is in-memory only — fine for the e2e harness and
    tests, but the events evaporate at process exit. ``PersistentTracer``
    bridges the existing recorder API into the ``traces`` sqlite table so
    production heartbeats leave a durable trail readable via the ``get_trace``
    MCP tool.

    All persistence is best-effort: if the sqlite write raises (busy, locked,
    schema mismatch), the in-memory append still happens and the goal tick
    continues — telemetry must never break production.

    Each tracer is scoped to one heartbeat tick: ``trace_id`` (uuid) groups
    every cognition / dispatch / delivery / etc. emitted during that tick;
    ``goal_id`` ties them to the goal whose tick they belong to.

    ``goals_dir`` (T0.5): when given, every cognition call recorded through
    this tracer ALSO writes a full transcript (prompt + response + usage
    header) to ``<goals_dir>/<goal_id>/transcripts/<utc-ts>-<role>.md`` and the
    trace row records the filename. This is the seam choice documented in the
    hardening plan: GoalService already holds ``goals_dir`` (the same value
    GoalStore is built from), so it plumbs the dir in at ``_make_tracer`` —
    one parameter, no new env resolution. ``None`` (the default) keeps the
    pre-T0.5 behavior: no transcript files.
    """

    def __init__(
        self,
        *,
        store: Any,  # devclaw.state_store.StateStore — typed as Any to avoid the cycle
        trace_id: str,
        goal_id: str,
        label: str = "",
        goals_dir: Optional[Path] = None,
    ) -> None:
        super().__init__(label=label)
        self._store = store
        self._trace_id = trace_id
        self._goal_id = goal_id
        self._goals_dir = Path(goals_dir) if goals_dir is not None else None

    def append(self, event: Any) -> None:
        super().append(event)
        payload = self.events[-1]
        try:
            self._store.append_trace_event(
                trace_id=self._trace_id,
                goal_id=self._goal_id,
                kind=str(payload.get("kind", "unknown")),
                payload=payload,
            )
        except Exception:
            # Telemetry must never break the cascade. Swallow + continue.
            pass

    def write_transcript(
        self, *, role: str, model: str, prompt: str, response: str,
        tokens_in: Optional[int] = None, tokens_out: Optional[int] = None,
        cost_usd: Optional[float] = None, error: str = "",
    ) -> str:
        """Write the full prompt + response of one cognition call to
        ``<goals_dir>/<goal_id>/transcripts/<utc-ts>-<role>.md`` and return the
        filename ("" when no goals_dir is bound). Best-effort like the sqlite
        mirror: any filesystem failure is swallowed — telemetry must never
        break the cascade."""
        if self._goals_dir is None or not self._goal_id:
            return ""
        try:
            tdir = self._goals_dir / self._goal_id / "transcripts"
            tdir.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            stamp = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z"
            safe_role = re.sub(r"[^A-Za-z0-9_-]+", "_", role or "unknown")
            path = tdir / f"{stamp}-{safe_role}.md"
            n = 2
            while path.exists():  # same role + same millisecond — vanishing, but cheap
                path = tdir / f"{stamp}-{safe_role}-{n}.md"
                n += 1
            t_in = str(tokens_in) if tokens_in is not None else f"~{len(prompt or '') // 4} (est)"
            t_out = str(tokens_out) if tokens_out is not None else f"~{len(response or '') // 4} (est)"
            cost = f"{cost_usd:.6f}" if cost_usd is not None else "n/a"
            header = [
                f"# cognition transcript — {role}",
                "",
                f"- ts: {now.isoformat(timespec='seconds')}",
                f"- role: {role}",
                f"- model: {model}",
                f"- goal_id: {self._goal_id}",
                f"- tokens_in: {t_in}",
                f"- tokens_out: {t_out}",
                f"- cost_usd: {cost}",
            ]
            if error:
                header.append(f"- error: {error}")
            body = "\n".join(header) + (
                f"\n\n## prompt\n\n{prompt or ''}\n\n## response\n\n{response or ''}\n"
            )
            path.write_text(body)
            return path.name
        except Exception:
            return ""


def now_ms() -> int:
    return int(time.monotonic() * 1000)
