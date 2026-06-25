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
    latency_ms: int = 0
    error: str = ""

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


# ---- recorders (no-ops when no tracer is attached) -------------------------


def record_cognition(
    *, role: str, model: str, prompt: str, response: str = "",
    latency_ms: int = 0, error: str = "",
) -> None:
    t = _current.get()
    if t is None:
        return
    t.append(CognitionEvent(
        role=role, model=model or "(default)",
        prompt_hash=_hash(prompt), prompt_preview=_preview(prompt),
        response_preview=_preview(response), latency_ms=latency_ms,
        error=error[:300],
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


def now_ms() -> int:
    return int(time.monotonic() * 1000)
