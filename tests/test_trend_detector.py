"""Integration tests for the trend-detector orchestrator.

Covers: signal iteration, cooldown round-trip via the meta table, LLM
retrospective with stubbed caller, trends.md write + auto-gitignore,
notifier hand-off, kill-switch + per-signal disable, and the per-heartbeat
fire cap.

Mirrors the test_goal_tick.py FakeClaude/RecordingNotifier pattern."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from devclaw import trend_detector as _td_mod
from devclaw.state_store import StateStore
from devclaw.trend_detector import TrendDetector, _TrendParseError
from devclaw.trend_signals import Signal, SignalContext, SignalResult


# ---- test doubles ---------------------------------------------------------


class _StubSignal(Signal):
    """Configurable signal — fires or not on demand, surfaces predictable evidence."""

    def __init__(
        self,
        signal_id: str,
        *,
        scope: str = "per_project",
        category: str = "drift",
        will_fire: bool = True,
        actual: float = 42.0,
        threshold: float = 10.0,
    ) -> None:
        self.id = signal_id
        self.scope = scope  # type: ignore[assignment]
        self.category = category  # type: ignore[assignment]
        self.will_fire = will_fire
        self._actual = actual
        self._threshold = threshold

    def check(self, ctx: SignalContext) -> SignalResult:
        return SignalResult(
            fired=self.will_fire,
            actual_value=self._actual,
            threshold_value=self._threshold,
            evidence={"finding": f"stub-{self.id}"},
            deeper_refs={"cmd": f"echo {self.id}"},
        )


class _CountingCaller:
    """Async claude_caller stub. Returns a canned JSON entry and counts calls."""

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or {
            "date": "2026-06-29T18:00:00+00:00",
            "signal": "STUB",
            "category": "drift",
            "observation": "stubbed observation",
            "evidence_refs": ["echo stub"],
            "proposed_action": "do the thing",
        }
        self.calls = 0
        self.last_prompt = ""

    async def __call__(self, prompt: str) -> str:
        self.calls += 1
        self.last_prompt = prompt
        return json.dumps(self.payload)


def _notifier():
    sent: list[dict] = []

    def send(payload: dict) -> None:
        sent.append(payload)

    return send, sent


def _detector_for(
    *, tmp_path: Path, signals: list[Signal], caller: _CountingCaller,
) -> tuple[TrendDetector, StateStore, list[dict], Path]:
    """Build a fresh detector with isolated state-store + harness-self file."""
    db = tmp_path / "test.db"
    store = StateStore(str(db))
    harness_file = tmp_path / "harness-trends.md"
    notify, sent = _notifier()
    detector = TrendDetector(
        state_store=store,
        goals_dir=tmp_path / "goals",
        claude_caller=caller,
        notifier_send=notify,
        signals=signals,
        harness_self_trends_path=harness_file,
        now_ms=lambda: 1750000000000,
    )
    return detector, store, sent, harness_file


# ---- per-goal fire path ---------------------------------------------------


@pytest.mark.asyncio
async def test_per_goal_fire_writes_entry_and_sets_cooldown(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    caller = _CountingCaller()
    detector, store, sent, _ = _detector_for(
        tmp_path=tmp_path,
        signals=[_StubSignal("S1", scope="per_project")],
        caller=caller,
    )

    await detector.run_per_goal(goal_id="g1", workspace_dir=str(workspace))

    # Entry was written.
    trends = workspace / ".devclaw" / "trends.md"
    assert trends.exists()
    content = trends.read_text()
    assert "S1" in content
    assert "stubbed observation" in content
    assert "do the thing" in content

    # Auto-gitignore wrote /.devclaw/.
    gi = workspace / ".gitignore"
    assert gi.exists()
    assert "/.devclaw/" in gi.read_text()

    # Cooldown was set in the meta table.
    cooldown_raw = store.get_trend_cooldown(f"project:{workspace}", "S1")
    assert cooldown_raw is not None
    assert int(cooldown_raw) > 1750000000000

    # Notifier received the structured payload.
    assert len(sent) == 1
    assert sent[0]["kind"] == "trend_observed"
    assert sent[0]["signal"] == "S1"

    # Exactly one LLM call.
    assert caller.calls == 1
    store.close()


@pytest.mark.asyncio
async def test_cooldown_silences_repeated_fires(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    caller = _CountingCaller()
    detector, store, sent, _ = _detector_for(
        tmp_path=tmp_path,
        signals=[_StubSignal("S1", scope="per_project")],
        caller=caller,
    )

    await detector.run_per_goal(goal_id="g1", workspace_dir=str(workspace))
    first_content = (workspace / ".devclaw" / "trends.md").read_text()
    assert caller.calls == 1

    # Second run: still inside the 24h cooldown. No LLM call, no second entry.
    await detector.run_per_goal(goal_id="g1", workspace_dir=str(workspace))
    assert caller.calls == 1
    assert (workspace / ".devclaw" / "trends.md").read_text() == first_content
    assert len(sent) == 1
    store.close()


@pytest.mark.asyncio
async def test_no_fire_when_signal_does_not_fire(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    caller = _CountingCaller()
    detector, store, sent, _ = _detector_for(
        tmp_path=tmp_path,
        signals=[_StubSignal("S1", scope="per_project", will_fire=False)],
        caller=caller,
    )

    await detector.run_per_goal(goal_id="g1", workspace_dir=str(workspace))

    # No entry, no LLM call, no notification, no cooldown.
    assert not (workspace / ".devclaw" / "trends.md").exists()
    assert caller.calls == 0
    assert sent == []
    assert store.get_trend_cooldown(f"project:{workspace}", "S1") is None
    store.close()


# ---- harness-self fire path ----------------------------------------------


@pytest.mark.asyncio
async def test_harness_self_fire_writes_to_configured_path(tmp_path):
    caller = _CountingCaller(payload={
        "date": "2026-06-29T18:00:00+00:00",
        "signal": "HSELF",
        "category": "harness_self",
        "observation": "harness-self stub fired",
        "evidence_refs": ["~/memory/goals"],
        "proposed_action": None,  # explicit null — no action needed
    })
    detector, store, sent, harness_file = _detector_for(
        tmp_path=tmp_path,
        signals=[_StubSignal("HSELF", scope="harness_self", category="harness_self")],
        caller=caller,
    )

    await detector.run_harness_self()

    assert harness_file.exists()
    content = harness_file.read_text()
    assert "HSELF" in content
    assert "harness-self stub fired" in content
    # proposed_action=null renders as "(none — pattern noted, no action recommended)"
    assert "no action recommended" in content
    assert sent[0]["scope"] == "harness_self"
    assert sent[0]["proposed_action"] is None
    store.close()


# ---- kill switches --------------------------------------------------------


@pytest.mark.asyncio
async def test_per_signal_disable_silences_named_signals(tmp_path, monkeypatch):
    """DEVCLAW_TREND_DISABLE comma-list silences specific signals without
    disabling the whole detector."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setattr(_td_mod, "TREND_DISABLE", {"S1"})
    caller = _CountingCaller()
    detector, store, sent, _ = _detector_for(
        tmp_path=tmp_path,
        signals=[_StubSignal("S1", scope="per_project")],
        caller=caller,
    )

    await detector.run_per_goal(goal_id="g1", workspace_dir=str(workspace))

    assert caller.calls == 0
    assert not (workspace / ".devclaw" / "trends.md").exists()
    assert sent == []
    store.close()


@pytest.mark.asyncio
async def test_master_kill_switch_silences_all_signals(tmp_path, monkeypatch):
    """DEVCLAW_TREND_ENABLED=0 disables every signal."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setattr(_td_mod, "TREND_ENABLED", False)
    caller = _CountingCaller()
    detector, store, sent, _ = _detector_for(
        tmp_path=tmp_path,
        signals=[_StubSignal("S1", scope="per_project"), _StubSignal("S2", scope="per_project")],
        caller=caller,
    )

    await detector.run_per_goal(goal_id="g1", workspace_dir=str(workspace))

    assert caller.calls == 0
    assert not (workspace / ".devclaw" / "trends.md").exists()
    store.close()


# ---- per-heartbeat fire cap ----------------------------------------------


@pytest.mark.asyncio
async def test_per_heartbeat_fire_cap_takes_highest_priority(tmp_path, monkeypatch):
    """When multiple signals would fire in the same scope on the same
    heartbeat, only the highest-priority one fires (the others wait for the
    next heartbeat). Priority order: D4 > R2 > everything else (v1 guess)."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    caller = _CountingCaller()
    # Both fire; priority order should pick D4 (idx 2) over R2 (idx 3).
    detector, store, sent, _ = _detector_for(
        tmp_path=tmp_path,
        signals=[
            _StubSignal("R2", scope="per_project"),
            _StubSignal("D4", scope="per_project"),
        ],
        caller=caller,
    )

    await detector.run_per_goal(goal_id="g1", workspace_dir=str(workspace))

    # Exactly ONE LLM call — the fire cap held.
    assert caller.calls == 1
    # The winner was D4 (higher priority).
    assert sent[0]["signal"] == "D4"
    # D4 cooldown was set; R2 cooldown was NOT — so R2 can fire next heartbeat.
    assert store.get_trend_cooldown(f"project:{workspace}", "D4") is not None
    assert store.get_trend_cooldown(f"project:{workspace}", "R2") is None
    store.close()


# ---- failure modes --------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_garbage_skips_entry_and_does_not_set_cooldown(tmp_path):
    """If the LLM returns un-JSON-parseable output, the detector skips the
    entry and does NOT set the cooldown (so the next heartbeat retries)."""
    workspace = tmp_path / "repo"
    workspace.mkdir()

    class _GarbageCaller:
        def __init__(self):
            self.calls = 0

        async def __call__(self, prompt: str) -> str:
            self.calls += 1
            return "not json at all, just prose"

    garbage = _GarbageCaller()
    detector, store, sent, _ = _detector_for(
        tmp_path=tmp_path,
        signals=[_StubSignal("S1", scope="per_project")],
        caller=garbage,  # type: ignore[arg-type]
    )

    await detector.run_per_goal(goal_id="g1", workspace_dir=str(workspace))

    assert garbage.calls == 1
    assert not (workspace / ".devclaw" / "trends.md").exists()
    assert sent == []
    assert store.get_trend_cooldown(f"project:{workspace}", "S1") is None
    store.close()


@pytest.mark.asyncio
async def test_signal_check_exception_is_isolated(tmp_path):
    """A signal that raises in check() must NOT crash the heartbeat. Other
    signals still get evaluated; the raising signal records a no-fire."""
    workspace = tmp_path / "repo"
    workspace.mkdir()

    class _ExplodingSignal(Signal):
        id = "BOOM"
        category = "drift"
        scope = "per_project"

        def check(self, ctx):
            raise RuntimeError("kaboom")

    caller = _CountingCaller()
    detector, store, sent, _ = _detector_for(
        tmp_path=tmp_path,
        signals=[_ExplodingSignal(), _StubSignal("OK", scope="per_project")],
        caller=caller,
    )

    # Must not raise.
    await detector.run_per_goal(goal_id="g1", workspace_dir=str(workspace))

    # The non-exploding signal still fired.
    assert caller.calls == 1
    assert sent[0]["signal"] == "OK"
    store.close()


# ---- entry parsing pins ---------------------------------------------------


# ---- bookmark management (PR2) -------------------------------------------


class _BookmarkAwareSignal(Signal):
    """Stub for bookmark-management tests — fires when configured."""

    id = "D1"  # match the priority entry so it isn't deprioritized
    category = "drift"
    scope = "per_project"
    advances_bookmark = True

    def __init__(self, will_fire: bool = True) -> None:
        self._will_fire = will_fire
        self.observed_bookmark: str | None = None

    def check(self, ctx):
        self.observed_bookmark = ctx.bookmark
        return SignalResult(
            fired=self._will_fire,
            actual_value=99.0, threshold_value=10.0,
            evidence={"finding": "ba-test"},
        )


@pytest.mark.asyncio
async def test_detector_seeds_bookmark_on_first_observation(tmp_path, monkeypatch):
    """First time the detector sees a workspace, it seeds the trend bookmark
    to current HEAD so bookmark-aware signals don't fire on full history."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setattr("devclaw.bookmark.git_head_sha", lambda wd: "f" * 40)
    sig = _BookmarkAwareSignal(will_fire=False)  # no fire, just verify seed
    caller = _CountingCaller()
    detector, store, _, _ = _detector_for(
        tmp_path=tmp_path, signals=[sig], caller=caller,
    )

    assert store.get_trend_bookmark(str(workspace)) is None
    await detector.run_per_goal(goal_id="g1", workspace_dir=str(workspace))
    # Bookmark was seeded.
    assert store.get_trend_bookmark(str(workspace)) == "f" * 40
    # Signal observed the seeded bookmark in ctx.
    assert sig.observed_bookmark == "f" * 40
    store.close()


@pytest.mark.asyncio
async def test_detector_advances_bookmark_after_fire_by_bookmark_aware_signal(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    # First call returns the seed; later calls return a new HEAD (post-fire advance).
    heads = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr("devclaw.bookmark.git_head_sha", lambda wd: next(heads))
    sig = _BookmarkAwareSignal(will_fire=True)
    caller = _CountingCaller()
    detector, store, _, _ = _detector_for(
        tmp_path=tmp_path, signals=[sig], caller=caller,
    )

    await detector.run_per_goal(goal_id="g1", workspace_dir=str(workspace))
    # Bookmark advanced from seed (a*40) to new HEAD (b*40) after fire.
    assert store.get_trend_bookmark(str(workspace)) == "b" * 40
    store.close()


@pytest.mark.asyncio
async def test_detector_does_not_advance_bookmark_for_non_bookmark_signal(tmp_path, monkeypatch):
    """A fire by R2/D4/H4 (advances_bookmark=False) leaves the bookmark
    untouched — D1/D2/D3's windows are protected from unrelated fires."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setattr("devclaw.bookmark.git_head_sha", lambda wd: "a" * 40)

    # R2-style signal that fires but doesn't advance bookmarks.
    class _PlainSig(Signal):
        id = "R2"
        category = "recurrence"
        scope = "per_project"
        advances_bookmark = False
        def check(self, ctx):
            return SignalResult(fired=True, actual_value=1, threshold_value=0)

    caller = _CountingCaller()
    detector, store, _, _ = _detector_for(
        tmp_path=tmp_path, signals=[_PlainSig()], caller=caller,
    )

    await detector.run_per_goal(goal_id="g1", workspace_dir=str(workspace))
    # Bookmark was seeded but NOT advanced — still equals the seed value.
    assert store.get_trend_bookmark(str(workspace)) == "a" * 40
    store.close()


@pytest.mark.asyncio
async def test_entry_signal_category_and_date_are_pinned_not_trusted_from_model(tmp_path):
    """The LLM can lie about ``signal``, ``category``, AND ``date`` in its
    JSON output — the detector pins all three to harness-supplied values.

    The date-pin landed after the 2026-06-29 live smoke caught the model
    stamping midnight instead of the actual fire time; without the override
    the entry timestamps drift away from when the fire really happened."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    caller = _CountingCaller(payload={
        "date": "2099-12-31T18:00:00+00:00",  # model lies about date too
        "signal": "WRONG",       # model lies
        "category": "wrong_cat", # model lies
        "observation": "obs",
        "evidence_refs": [],
        "proposed_action": None,
    })
    detector, store, sent, _ = _detector_for(
        tmp_path=tmp_path,
        signals=[_StubSignal("S1", scope="per_project", category="drift")],
        caller=caller,
    )

    await detector.run_per_goal(goal_id="g1", workspace_dir=str(workspace))

    content = (workspace / ".devclaw" / "trends.md").read_text()
    # Pinned signal/category land in the header.
    assert "S1 — drift" in content
    assert "WRONG" not in content
    assert "wrong_cat" not in content
    # Date pin: the model's 2099 fabrication does NOT appear; the detector's
    # harness-supplied now_ms (lambda: 1750000000000) wins.
    assert "2099" not in content
    assert sent[0]["signal"] == "S1"
    store.close()


# ---- read_trends_text helper (trend-PR3) ----------------------------------


def test_read_trends_text_missing_file_returns_placeholder(tmp_path):
    # No .devclaw/trends.md exists at the workspace yet.
    text = _td_mod.read_trends_text(str(tmp_path))
    assert text == "(no trends recorded for this scope yet)"


def test_read_trends_text_returns_file_contents(tmp_path):
    devclaw_dir = tmp_path / ".devclaw"
    devclaw_dir.mkdir()
    body = "# trends\n\n## [2026-06-29] R2 — recurrence\n\nobservation body\n"
    (devclaw_dir / "trends.md").write_text(body)
    text = _td_mod.read_trends_text(str(tmp_path))
    assert text == body


def test_read_trends_text_tail_truncates_when_over_limit(tmp_path):
    devclaw_dir = tmp_path / ".devclaw"
    devclaw_dir.mkdir()
    # 5kB of filler followed by a tail marker — we want the tail kept, head dropped.
    body = ("X" * 5000) + "\nTAIL_MARKER\n"
    (devclaw_dir / "trends.md").write_text(body)
    text = _td_mod.read_trends_text(str(tmp_path), limit_chars=100)
    assert len(text) == 100
    assert "TAIL_MARKER" in text
    assert "X" * 5000 not in text
