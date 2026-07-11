"""Trend detector — the cross-session engineering practice (designed 2026-06-29).

The orchestrator that runs every heartbeat: it iterates the enabled signals,
honors per-signal cooldowns (persisted in the sqlite ``meta`` table), picks at
most one fired signal per scope per heartbeat (priority-ordered), and — when
something fires — invokes the LLM retrospective pass and writes one structured
entry to ``trends.md``.

The mechanism / cognition split applies recursively here: the *pre-filter* is
pure Python (zero tokens, runs unconditionally each heartbeat); the *cognition*
runs only when a pre-filter fires. An idle or healthy project produces zero
trends.md entries — and that's correct behavior, not a bug.

Boundary (structurally enforced by the constructor — the detector simply has no
handle to anything it shouldn't write to):
  * NEVER edits ``AGENTS.md``, never creates goals, never alters ``done_when``.
  * Writes ONLY to: ``trends.md`` (per-project or harness-self), the sqlite
    ``meta`` table (cooldown timestamps), the ``traces`` table (observability),
    and the injected notifier.
  * The boundary check ``trend_detector_only_has_safe_writes`` (Stage 5 test)
    pins this by inspecting the constructor's parameter set.

Hook points (Stage 4 wires these — Stage 1 just exposes the methods):
  * ``run_per_goal`` — inside ``goal/tick.py`` per-goal loop, where
    ``workspace_dir`` and the tracer are already in scope.
  * ``run_harness_self`` — once globally after the per-goal loop.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .loom import trace as _trace
from .planner import extract_json
from .state_store import StateStore
from .trend_signals import Scope, Signal, SignalContext, SignalResult, all_signals


class _TrendParseError(Exception):
    """Raised when the LLM retrospective pass returns un-JSON-parseable output.
    Caught by ``_fire`` — a parse failure means we skip writing an entry for
    this fire (no cooldown set either, so the next heartbeat may retry)."""


def _iso_now(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _signal_description(signal: Signal) -> str:
    """First non-empty line of the signal's class docstring — the prompt uses
    it to brief the model on what this particular signal means."""
    doc = (signal.__class__.__doc__ or "").strip()
    for line in doc.splitlines():
        s = line.strip()
        if s:
            return s
    return "(no description)"

ClaudeCaller = Callable[[str], Awaitable[str]]
NotifierSend = Callable[[dict], None]

# ---- env vars (read inline; matches the rest of devclaw) -------------------

#: Model tier for the retrospective pass. ``sonnet`` by default — judging
#: bounded evidence against thresholds doesn't need opus.
from .model_tiers import model_for as _model_for
TREND_MODEL = _model_for("trend")

#: Kill switch for the whole discipline. Set to ``0`` to disable.
TREND_ENABLED = os.environ.get("DEVCLAW_TREND_ENABLED", "1") != "0"

#: Comma-separated signal IDs to silence without redeploying — e.g.
#: ``DEVCLAW_TREND_DISABLE=R2,H4`` mutes those two while calibration is in flight.
TREND_DISABLE = {
    s.strip().upper()
    for s in os.environ.get("DEVCLAW_TREND_DISABLE", "").split(",")
    if s.strip()
}

#: Where the harness-self trends file lives. Defaults into Denys's vault.
HARNESS_SELF_TRENDS_PATH = Path(
    os.environ.get("DEVCLAW_TREND_HARNESS_SELF_FILE", "~/memory/projects/devclaw/trends.md")
).expanduser()

#: Default cooldown when a signal doesn't set its own.
COOLDOWN_HOURS_DEFAULT = 24

# Per-heartbeat fire cap: if multiple signals would fire in the same scope on
# the same heartbeat, take the highest priority. The others wait — cooldown
# only sets on the one that actually fires, so the deferred ones can fire next
# heartbeat. Order is a v1 guess; calibratable.
_SIGNAL_PRIORITY: list[str] = [
    "D3", "H2", "D4", "R2", "H4",
    "R1", "D1", "D2", "G1", "G3",
]


def _default_now_ms() -> int:
    return int(time.time() * 1000)


def _priority_index(signal_id: str) -> int:
    """Lower number = higher priority. Unknown signals sort last."""
    try:
        return _SIGNAL_PRIORITY.index(signal_id)
    except ValueError:
        return len(_SIGNAL_PRIORITY)


def _scope_key_for_project(workspace_dir: str) -> str:
    """The cooldown key namespace for a per-project scope. Workspace path is
    the natural identity — two goals in the same repo share cooldowns."""
    return f"project:{workspace_dir}"


_HARNESS_SELF_SCOPE_KEY = "harness_self"


def read_trends_text(scope: str, limit_chars: int = 5000) -> str:
    """Read the tail of ``trends.md`` for a scope and return raw text.

    ``scope='harness_self'`` → the global harness-self file. Anything else is
    treated as a workspace path → ``<scope>/.devclaw/trends.md``.

    Tail-truncated to ``limit_chars`` (trends files only grow; recent is what
    matters for next-tick consumption). Returns a placeholder string when the
    file is missing or unreadable — callers that want a "no trends → skip
    section" UX should check against the placeholder OR pass the return to a
    section that guards on truthiness of real content. ``GoalService.read_trends``
    wraps this for the ``review_trends`` MCP tool (which expects the placeholder
    text so the human reader sees the discipline is wired)."""
    if scope == "harness_self":
        path = HARNESS_SELF_TRENDS_PATH
    else:
        path = Path(scope) / ".devclaw" / "trends.md"
    if not path.exists():
        return "(no trends recorded for this scope yet)"
    try:
        raw = path.read_text()
    except OSError as exc:
        return f"(could not read {path}: {exc})"
    return raw[-limit_chars:] if len(raw) > limit_chars else raw


class TrendDetector:
    """Heartbeat-driven orchestrator. See module docstring for boundary rules
    and hook points. The class is intentionally narrow — the only writes it can
    perform are determined by what's passed to the constructor."""

    def __init__(
        self,
        *,
        state_store: StateStore,
        goals_dir: Path,
        claude_caller: ClaudeCaller,
        notifier_send: NotifierSend,
        signals: Optional[list[Signal]] = None,
        harness_self_trends_path: Path = HARNESS_SELF_TRENDS_PATH,
        now_ms: Callable[[], int] = _default_now_ms,
    ) -> None:
        self._store = state_store
        self._goals_dir = Path(goals_dir)
        self._caller = claude_caller
        self._notify = notifier_send
        self._signals = signals if signals is not None else all_signals()
        self._harness_self_path = harness_self_trends_path
        self._now_ms = now_ms

    # ---- enablement + cooldown gates ---------------------------------------

    def _is_signal_enabled(self, signal: Signal) -> bool:
        if not TREND_ENABLED:
            return False
        return signal.id.upper() not in TREND_DISABLE

    def _in_cooldown(self, scope_key: str, signal: Signal) -> bool:
        raw = self._store.get_trend_cooldown(scope_key, signal.id)
        if not raw:
            return False
        try:
            until = int(raw)
        except ValueError:
            return False
        return self._now_ms() < until

    def _set_cooldown(self, scope_key: str, signal: Signal) -> None:
        hours = signal.cooldown_hours or COOLDOWN_HOURS_DEFAULT
        until = self._now_ms() + hours * 3600 * 1000
        self._store.set_trend_cooldown(scope_key, signal.id, str(until))

    def _fingerprint_matches_last(
        self, scope_key: str, signal: Signal, result: SignalResult,
    ) -> bool:
        """True iff this fire is telling the same story as the last successful
        fire — same file + same commits + same everything a signal calls
        identity.

        Added 2026-07-03 after audit found signals firing daily on identical
        evidence: time-cooldown expired (24h), evidence hadn't changed, LLM
        was called, wrote a "no new evidence" retrospective, notified the
        owner, cooldown reset, repeat. This is the second gate: suppress
        the fire entirely when the underlying situation hasn't changed."""
        try:
            new_fp = signal.fingerprint(result)
        except Exception:  # noqa: BLE001 — signal fingerprint bugs must not break tick
            return False
        if not new_fp:
            return False
        last = self._store.get_trend_fingerprint(scope_key, signal.id)
        return last == new_fp

    def _set_fingerprint(
        self, scope_key: str, signal: Signal, result: SignalResult,
    ) -> None:
        """Record this fire's identity so a future identical fire suppresses.
        Silent on signal-side fingerprint errors (matches the check pathway)."""
        try:
            fp = signal.fingerprint(result)
        except Exception:  # noqa: BLE001
            return
        if fp:
            self._store.set_trend_fingerprint(scope_key, signal.id, fp)

    # ---- public entry points (Stage 4 wires these into tick.py) ------------

    async def run_per_goal(self, *, goal_id: str, workspace_dir: str) -> None:
        """Run all per-project signals scoped to one goal's workspace. Called
        inside ``goal/tick.py``'s per-goal loop. Inherits the active tracer."""
        from .bookmark import git_head_sha

        scope_key = _scope_key_for_project(workspace_dir)
        # Seed the trend bookmark on first observation of this workspace so
        # bookmark-aware signals (D1/D2/D3) don't fire spuriously on full
        # repo history. After this seed, bookmark-aware signals will see
        # bookmark == HEAD and return no-fire until something changes.
        bookmark = self._store.get_trend_bookmark(workspace_dir)
        if bookmark is None:
            seeded = git_head_sha(workspace_dir)
            if seeded is not None:
                self._store.set_trend_bookmark(workspace_dir, seeded)
                bookmark = seeded
        ctx = SignalContext(
            scope="per_project",
            workspace_dir=workspace_dir,
            goal_id=goal_id,
            goals_dir=self._goals_dir,
            now_ms=self._now_ms(),
            bookmark=bookmark,
        )
        await self._run_signals(
            scope_key=scope_key,
            scope_label="per_project",
            ctx=ctx,
            signals=[s for s in self._signals if s.scope == "per_project"],
        )

    async def run_harness_self(self) -> None:
        """Run harness-self signals once per heartbeat. Called after the
        per-goal loop completes in ``goal/tick.py``."""
        ctx = SignalContext(
            scope="harness_self",
            workspace_dir=None,
            goal_id=None,
            goals_dir=self._goals_dir,
            now_ms=self._now_ms(),
        )
        await self._run_signals(
            scope_key=_HARNESS_SELF_SCOPE_KEY,
            scope_label="harness_self",
            ctx=ctx,
            signals=[s for s in self._signals if s.scope == "harness_self"],
        )

    # ---- core loop ---------------------------------------------------------

    async def _run_signals(
        self,
        *,
        scope_key: str,
        scope_label: Scope,
        ctx: SignalContext,
        signals: list[Signal],
    ) -> None:
        """Iterate signals through the pre-filter, then fire the
        highest-priority candidate (per-heartbeat fire cap of 1)."""
        candidates: list[tuple[Signal, SignalResult]] = []
        for signal in signals:
            if not self._is_signal_enabled(signal):
                _trace.record_trend_check(
                    signal=signal.id, scope=scope_label,
                    fired=False, reason="disabled",
                )
                continue
            if self._in_cooldown(scope_key, signal):
                _trace.record_trend_check(
                    signal=signal.id, scope=scope_label,
                    fired=False, reason="cooldown",
                )
                continue
            try:
                result = signal.check(ctx)
            except Exception as exc:
                # Signal failures must never break the heartbeat.
                _trace.record_trend_check(
                    signal=signal.id, scope=scope_label,
                    fired=False, reason=f"error:{exc.__class__.__name__}",
                )
                continue
            fingerprint_dupe = (
                result.fired
                and self._fingerprint_matches_last(scope_key, signal, result)
            )
            _trace.record_trend_check(
                signal=signal.id, scope=scope_label,
                fired=result.fired and not fingerprint_dupe,
                actual=result.actual_value,
                threshold=result.threshold_value,
                reason=(
                    "fingerprint_dupe"
                    if fingerprint_dupe
                    else "fired"
                    if result.fired
                    else "below_threshold"
                ),
            )
            if result.fired and not fingerprint_dupe:
                candidates.append((signal, result))

        if not candidates:
            return

        candidates.sort(key=lambda sr: _priority_index(sr[0].id))
        signal, result = candidates[0]
        await self._fire(signal, result, ctx, scope_key, scope_label)

    async def _fire(
        self,
        signal: Signal,
        result: SignalResult,
        ctx: SignalContext,
        scope_key: str,
        scope_label: Scope,
    ) -> None:
        """Build payload → LLM retrospective → parse → write entry → cooldown →
        notify. Failures of cognition or parse are recorded and skipped (no
        cooldown set so the next heartbeat retries); failures of write or
        notify are recorded but still set the cooldown (we don't want a write
        glitch to trigger the same fire every tick)."""
        trends_path = self._trends_path_for(ctx)
        recent_excerpt = self._read_recent_trends_excerpt(trends_path)
        prompt = self._build_prompt(signal, result, ctx, scope_label, recent_excerpt)

        try:
            raw = await self._caller(prompt)
        except Exception as exc:  # noqa: BLE001 — telemetry-shaped catch-all
            _trace.record_note(
                f"trend_detector: LLM call failed for {signal.id}: {exc.__class__.__name__}: {exc}"
            )
            return

        try:
            entry = self._parse_entry(raw, signal)
        except _TrendParseError as exc:
            _trace.record_note(f"trend_detector: parse failed for {signal.id}: {exc}")
            return

        try:
            self._append_entry(trends_path, entry, scope_label)
            if ctx.scope == "per_project" and ctx.workspace_dir:
                self._ensure_gitignore(ctx.workspace_dir)
        except OSError as exc:
            _trace.record_note(f"trend_detector: write failed for {signal.id}: {exc}")

        self._set_cooldown(scope_key, signal)
        # Record the identity of this fire so the next tick suppresses when
        # the situation hasn't changed. Complements (not replaces) the
        # time cooldown: cooldown is "don't check for N hours"; fingerprint
        # is "don't fire again on the same story ever, until real change."
        self._set_fingerprint(scope_key, signal, result)

        # Bookmark-aware signals reset the workspace's observation window
        # after firing — next heartbeat compares against the new HEAD instead
        # of stacking up further. Non-bookmark signals (R2, D4, H4) leave the
        # bookmark alone so they don't disturb D1/D2/D3's windows.
        if getattr(signal, "advances_bookmark", False) and ctx.workspace_dir:
            from .bookmark import git_head_sha

            head = git_head_sha(ctx.workspace_dir)
            if head is not None:
                self._store.set_trend_bookmark(ctx.workspace_dir, head)

        try:
            self._notify({
                "kind": "trend_observed",
                "signal": entry["signal"],
                "category": entry["category"],
                "observation": entry["observation"],
                "proposed_action": entry.get("proposed_action"),
                "scope": scope_label,
                "path": str(trends_path),
            })
        except Exception as exc:  # noqa: BLE001
            _trace.record_note(f"trend_detector: notify failed for {signal.id}: {exc}")

    # ---- persistence + prompt helpers -------------------------------------

    def _trends_path_for(self, ctx: SignalContext) -> Path:
        """The trends.md location for this scope. Per-project: a hidden dir at
        the workspace root, gitignored. Harness-self: the configured file
        (defaults into Denys's vault)."""
        if ctx.scope == "harness_self":
            return self._harness_self_path
        if not ctx.workspace_dir:
            raise ValueError("per_project scope requires workspace_dir")
        return Path(ctx.workspace_dir) / ".devclaw" / "trends.md"

    def _read_recent_trends_excerpt(self, path: Path, chars: int = 3000) -> str:
        """The tail of the trends.md (raw markdown — no parsing). Used as
        prompt context so the LLM doesn't repeat itself across firings."""
        if not path.exists():
            return "(no prior trends)"
        try:
            text = path.read_text()
        except OSError:
            return "(could not read trends file)"
        return text[-chars:] if len(text) > chars else text

    def _build_prompt(
        self,
        signal: Signal,
        result: SignalResult,
        ctx: SignalContext,
        scope_label: Scope,
        recent_excerpt: str,
    ) -> str:
        """The trend retrospective prompt. Inlined (short + self-contained);
        externalize to ``devclaw/prompts/`` if/when it grows."""
        if scope_label == "per_project":
            scope_info = (
                f"per-project scope\n"
                f"workspace_dir: {ctx.workspace_dir}\n"
                f"goal_id: {ctx.goal_id}"
            )
        else:
            scope_info = (
                "harness-self scope (devclaw observing its own behavior across all goals)"
            )

        return f"""You are devclaw's trend detector — the cross-session retrospective. A
deterministic pre-filter just FIRED on signal {signal.id}. Your job is to
write ONE structured observation entry that future-Denys (and future devclaw
runs) will read.

## What just fired

- signal: {signal.id} ({signal.category})
- description: {_signal_description(signal)}
- actual value: {result.actual_value}
- threshold: {result.threshold_value}

## Bounded evidence (gathered by the pre-filter)

{json.dumps(result.evidence, indent=2, default=str)}

## Deeper refs (paths / commands you can mentally consult)

{json.dumps(result.deeper_refs, indent=2, default=str)}

## Scope

{scope_info}

## Recent prior trends (DO NOT repeat what's already noted)

{recent_excerpt}

## How to judge

Distinguish between:
- A REAL pattern worth correcting (e.g. the same module is genuinely a recurring
  bug-source, AGENTS.md genuinely needs updating, steering frequency reflects a
  real misalignment).
- A BENIGN pre-filter artifact (e.g. CHANGELOG.md naturally touched on every fix
  commit is not a "hotspot" in the recurrence sense; a project that never had
  an AGENTS.md may not yet need one if it's tiny).

If real → propose ONE concrete action (e.g. "add 'every PR must include tests'
to AGENTS.md", "consider refactoring file X — it has accumulated 14 fix-class
commits"). If benign → ``proposed_action`` MUST be null.

Be neutral, terse, and specific. 1-3 sentences for the observation.

## Output

Return JSON ONLY — no prose around it, no markdown fences:

{{
  "date": "<ISO 8601 timestamp, e.g. 2026-06-29T15:00:00+00:00>",
  "signal": "{signal.id}",
  "category": "{signal.category}",
  "observation": "<1-3 sentences>",
  "evidence_refs": ["<paths or commands you'd point a reader to>"],
  "proposed_action": <null | "<one concrete action sentence>">
}}

Return the JSON now.
"""

    def _parse_entry(self, raw: str, signal: Signal) -> dict:
        """Validate + normalize the LLM's entry. Pins ``signal`` / ``category``
        to what the pre-filter actually fired (the model cannot lie about
        these). Defaults missing fields. Raises ``_TrendParseError`` only on
        hard failure (no JSON at all)."""
        try:
            json_text = extract_json(raw)
        except Exception as exc:  # noqa: BLE001 — defensive
            raise _TrendParseError(f"no JSON found in LLM output: {exc}") from exc
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise _TrendParseError(f"invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise _TrendParseError("entry must be a JSON object")

        # Pin the load-bearing fields to what the pre-filter said. The model
        # can technically supply ``signal`` / ``category`` / ``date`` in its
        # JSON; we override all three. Live smoke 2026-06-29 caught the model
        # stamping the date at midnight when the fire actually happened at
        # 12:05 — pinning prevents that drift from accumulating across entries.
        parsed["signal"] = signal.id
        parsed["category"] = signal.category
        parsed["date"] = _iso_now(self._now_ms())
        if not isinstance(parsed.get("observation"), str) or not parsed["observation"].strip():
            parsed["observation"] = "(no observation provided)"
        evidence_refs = parsed.get("evidence_refs")
        if not isinstance(evidence_refs, list):
            evidence_refs = [str(evidence_refs)] if evidence_refs else []
        parsed["evidence_refs"] = [str(r) for r in evidence_refs if str(r).strip()]
        action = parsed.get("proposed_action")
        if action in (None, "", "null", "None"):
            parsed["proposed_action"] = None
        else:
            parsed["proposed_action"] = str(action).strip() or None
        return parsed

    def _append_entry(self, path: Path, entry: dict, scope_label: Scope) -> None:
        """Append one entry to the trends.md file. Creates parent dirs + file
        header on first write. Mirrors the GoalStore.append_steering shape:
        append-only, simple markdown, monotonically grows."""
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        scope_human = "harness-self" if scope_label == "harness_self" else "per-project"
        with path.open("a") as fh:
            if is_new:
                fh.write(f"# trends — devclaw trend detector ({scope_human})\n\n")
                fh.write(
                    "Auto-generated by devclaw's trend detector. Each entry is ONE\n"
                    "observation surfaced by a deterministic pre-filter and judged\n"
                    "by a single retrospective LLM pass. Promote findings to\n"
                    "AGENTS.md (or the project's plan) when warranted — the detector\n"
                    "observes; the human encodes.\n\n"
                )
            fh.write(f"## [{entry['date']}] {entry['signal']} — {entry['category']}\n\n")
            fh.write(f"{entry['observation']}\n\n")
            if entry["evidence_refs"]:
                fh.write("**Evidence:**\n")
                for ref in entry["evidence_refs"]:
                    fh.write(f"- `{ref}`\n")
                fh.write("\n")
            action = entry.get("proposed_action")
            if action:
                fh.write(f"**Proposed action:** {action}\n\n")
            else:
                fh.write("**Proposed action:** _(none — pattern noted, no action recommended)_\n\n")
            fh.write("---\n\n")

    def _ensure_gitignore(self, workspace_dir: str) -> None:
        """Auto-add ``/.devclaw/`` to the workspace's ``.gitignore`` if missing.
        ``trends.md`` is detector output (mechanism), not standards (AGENTS.md
        is the standard); it doesn't travel committed with the repo. Write
        failures are swallowed — the trend pass already wrote the entry, the
        .gitignore touch is a politeness."""
        workspace = Path(workspace_dir)
        gitignore = workspace / ".gitignore"
        accepted_forms = {"/.devclaw/", ".devclaw/", "/.devclaw", ".devclaw"}
        try:
            if gitignore.exists():
                content = gitignore.read_text()
                for line in content.splitlines():
                    if line.strip() in accepted_forms:
                        return
                with gitignore.open("a") as fh:
                    if content and not content.endswith("\n"):
                        fh.write("\n")
                    fh.write("/.devclaw/\n")
            else:
                gitignore.write_text("/.devclaw/\n")
        except OSError:
            return
