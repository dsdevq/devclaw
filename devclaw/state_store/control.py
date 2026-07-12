"""Control-plane meta wrappers — the typed helpers over the ``meta`` key/value
table: the account-wide quota pause, the operator hold, per-goal run windows,
per-workspace circuit-breakers, and the trend-detector cooldown/fingerprint/
bookmark state.

Split out of ``StateStore`` as a mixin on the SAME instance — every method here
runs against the ``self._db`` / ``self._lock`` / ``self._commit`` the core store
owns, so the single-connection / single-writer semantics are byte-identical to
the pre-split monolith.
"""

from __future__ import annotations

import json
from typing import Optional


class ControlPlaneMixin:
    # ---- meta / global flags (the quota pause) ---------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._commit()

    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def delete_meta(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM meta WHERE key = ?", (key,))
            self._commit()

    def list_meta_keys(self, prefix: str = "") -> list[str]:
        """Meta keys, optionally filtered to those starting with ``prefix``. Used
        to enumerate per-goal run-windows (``run_schedule:<goal_id>``)."""
        with self._lock:
            if prefix:
                rows = self._db.execute(
                    "SELECT key FROM meta WHERE key LIKE ? ESCAPE '\\'",
                    (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
                ).fetchall()
            else:
                rows = self._db.execute("SELECT key FROM meta").fetchall()
        return [r["key"] for r in rows]

    def set_global_pause(self, until_ms: int, reason: str) -> None:
        """Pause ALL dispatch until ``until_ms`` (epoch ms) — the whole OAuth quota
        is account-wide, so a limit on one task pauses everything. Persisted so a
        restart still honours it."""
        self.set_meta("pause_until_ms", str(int(until_ms)))
        self.set_meta("pause_reason", reason or "")

    def global_pause(self) -> tuple[int, str]:
        """Return (until_ms, reason). until_ms is 0 when no pause is set."""
        raw = self.get_meta("pause_until_ms")
        try:
            until = int(raw) if raw else 0
        except ValueError:
            until = 0
        return until, (self.get_meta("pause_reason") or "")

    def clear_global_pause(self) -> None:
        self.delete_meta("pause_until_ms")
        self.delete_meta("pause_reason")

    # The pause-NOTIFIED flag lives beside the pause but is NOT cleared by
    # clear_global_pause on purpose: either layer (task queue or goal tick) may
    # lazily clear an expired pause first, and the resume notification must
    # still fire exactly once afterwards — the goal tick owns the flag's
    # lifecycle (set on the pause ping, cleared on the resume ping).

    def set_pause_notified(self, on: bool) -> None:
        """Record (``on=True``) / reset (``on=False``) that the owner was told
        about the current global pause, so they're pinged once per pause and
        once on resume — not every tick."""
        if on:
            self.set_meta("pause_notified", "1")
        else:
            self.delete_meta("pause_notified")

    def pause_notified(self) -> bool:
        """Whether the owner has already been pinged about the current pause."""
        return self.get_meta("pause_notified") == "1"

    # ---- operator dispatch controls (manual pause + daily run window) ----
    # Human-facing siblings of the quota pause above. Distinct meta keys, so the
    # automatic quota pause expiring/clearing never lifts a hold a person set on
    # purpose (and vice-versa). Read by ``dispatch_gate`` at both heartbeat gates.

    def set_operator_hold(self, on: bool, reason: str = "") -> None:
        """Manually pause (``on=True``) or resume (``on=False``) ALL new dispatch."""
        if on:
            self.set_meta("operator_hold", json.dumps({"on": True, "reason": reason or ""}))
        else:
            self.delete_meta("operator_hold")

    def operator_hold(self) -> tuple[bool, str]:
        """Return ``(on, reason)``. ``(False, "")`` when no hold is set."""
        raw = self.get_meta("operator_hold")
        if not raw:
            return False, ""
        try:
            data = json.loads(raw)
            return bool(data.get("on")), str(data.get("reason") or "")
        except (ValueError, TypeError):
            return False, ""

    #: meta-key prefix for a per-goal run-window. The global window keeps the bare
    #: ``run_schedule`` key; a goal's own window is ``run_schedule:<goal_id>``.
    _GOAL_SCHEDULE_PREFIX = "run_schedule:"

    def _schedule_key(self, goal_id: "str | None") -> str:
        return "run_schedule" if not goal_id else f"{self._GOAL_SCHEDULE_PREFIX}{goal_id}"

    def set_run_schedule(
        self, enabled: bool, start: str, end: str, tz: str, goal_id: "str | None" = None
    ) -> None:
        """Daily window during which dispatch is allowed. Outside it, new dispatch
        is gated (in-flight finishes). ``start``/``end`` are ``'HH:MM'`` in ``tz``.

        With ``goal_id`` set this writes a PER-GOAL window (an extra narrowing on
        top of the global one), stored under ``run_schedule:<goal_id>``; without
        it, the engine-wide window."""
        self.set_meta(self._schedule_key(goal_id), json.dumps(
            {"enabled": bool(enabled), "start": start, "end": end, "tz": tz}
        ))

    def clear_run_schedule(self, goal_id: "str | None" = None) -> None:
        """Remove a schedule so it stops restricting dispatch (a per-goal window
        cleared this way falls back to the global window only)."""
        self.delete_meta(self._schedule_key(goal_id))

    def get_run_schedule(self, goal_id: "str | None" = None) -> dict:
        """The run-schedule dict; a disabled 09:00–18:00 Europe/Kyiv default when
        none is set (or the stored value is corrupt). Shape mirrors
        ``dispatch_gate.DEFAULT_SCHEDULE``. With ``goal_id`` set, returns that
        goal's own window (disabled-default when it has none — the global window
        is applied separately at the outer gate, so an unset per-goal window must
        add no restriction)."""
        from ..dispatch_gate import DEFAULT_SCHEDULE
        raw = self.get_meta(self._schedule_key(goal_id))
        if not raw:
            return dict(DEFAULT_SCHEDULE)
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return dict(DEFAULT_SCHEDULE)
        return {
            "enabled": bool(data.get("enabled")),
            "start": str(data.get("start") or DEFAULT_SCHEDULE["start"]),
            "end": str(data.get("end") or DEFAULT_SCHEDULE["end"]),
            "tz": str(data.get("tz") or DEFAULT_SCHEDULE["tz"]),
        }

    def list_goal_schedules(self) -> dict[str, dict]:
        """Every per-goal window keyed by goal_id (skips the global one). Lets the
        console/control surface show which goals carry their own window."""
        out: dict[str, dict] = {}
        for key in self.list_meta_keys(prefix=self._GOAL_SCHEDULE_PREFIX):
            goal_id = key[len(self._GOAL_SCHEDULE_PREFIX):]
            if goal_id:
                out[goal_id] = self.get_run_schedule(goal_id)
        return out

    # ---- workspace circuit-breaker (per-workspace pause) -----------------

    def count_recent_task_failures(self, workspace_dir: str, since_ms: int) -> int:
        """Number of tasks that failed for one workspace since ``since_ms``.
        Used by the circuit-breaker to trip a per-workspace hold when a run of
        failures piles up in a short window (the 2026-07-02 quota-burn pattern:
        one broken workspace keeps re-attempting until Denys notices)."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM tasks "
                "WHERE workspace_dir = ? AND status = 'failed' "
                "AND completed_at IS NOT NULL AND completed_at >= ?",
                (workspace_dir, since_ms),
            ).fetchone()
        return int(row["n"])

    def set_workspace_break(
        self, workspace_dir: str, until_ms: int, reason: str
    ) -> None:
        """Hold dispatch for ONE workspace until ``until_ms`` (epoch ms). Sibling
        of the global quota pause but scoped — other workspaces keep running."""
        self.set_meta(
            f"workspace_break:{workspace_dir}",
            json.dumps({"until_ms": int(until_ms), "reason": reason or ""}),
        )

    def get_workspace_break(self, workspace_dir: str) -> tuple[int, str]:
        """Return (until_ms, reason). until_ms is 0 when no break is set."""
        raw = self.get_meta(f"workspace_break:{workspace_dir}")
        if not raw:
            return 0, ""
        try:
            data = json.loads(raw)
            return int(data.get("until_ms") or 0), str(data.get("reason") or "")
        except (ValueError, TypeError):
            return 0, ""

    def clear_workspace_break(self, workspace_dir: str) -> None:
        self.delete_meta(f"workspace_break:{workspace_dir}")

    def list_workspace_breaks(self) -> list[tuple[str, int, str]]:
        """All currently-recorded workspace breaks (may include expired ones —
        the caller filters). Read surface for observability + ops-agent."""
        prefix = "workspace_break:"
        with self._lock:
            rows = self._db.execute(
                "SELECT key, value FROM meta WHERE key LIKE ?", (f"{prefix}%",)
            ).fetchall()
        out: list[tuple[str, int, str]] = []
        for r in rows:
            ws = r["key"][len(prefix):]
            try:
                data = json.loads(r["value"])
                out.append((ws, int(data.get("until_ms") or 0), str(data.get("reason") or "")))
            except (ValueError, TypeError):
                continue
        return out

    # ---- trend-detector cooldowns (typed wrappers over set_meta/get_meta) -

    def set_trend_cooldown(self, scope: str, signal_id: str, until_ms_str: str) -> None:
        """Persist the cooldown for one (scope, signal) pair. ``until_ms_str``
        is epoch milliseconds as a string — same shape as ``pause_until_ms``,
        so the trend detector reuses the meta table instead of inventing a
        per-repo JSON file that would recreate the write-concurrency cliff
        WAL already solved."""
        self.set_meta(f"trend_cooldown:{scope}:{signal_id}", until_ms_str)

    def get_trend_cooldown(self, scope: str, signal_id: str) -> Optional[str]:
        """The cooldown for one (scope, signal) pair, or ``None`` if no
        cooldown was set / has been cleared."""
        return self.get_meta(f"trend_cooldown:{scope}:{signal_id}")

    def set_trend_fingerprint(self, scope: str, signal_id: str, fp: str) -> None:
        """Persist the fingerprint (identity hash of the situation) of the
        LAST successful fire for one (scope, signal) pair. Added 2026-07-03
        after audit found R2 firing 4 days consecutively on identical evidence
        because the time-cooldown expired without any new data. The detector
        now compares new fires against this fingerprint and suppresses when
        the story hasn't changed. Distinct from cooldown (which is a wall-
        clock timer); this is content identity."""
        self.set_meta(f"trend_fingerprint:{scope}:{signal_id}", fp)

    def get_trend_fingerprint(self, scope: str, signal_id: str) -> Optional[str]:
        """The last-fire fingerprint for one (scope, signal) pair. ``None``
        when the signal has never fired at that scope (fresh fire allowed)."""
        return self.get_meta(f"trend_fingerprint:{scope}:{signal_id}")

    def set_trend_bookmark(self, workspace_dir: str, sha: str) -> None:
        """Persist the trend detector's last-seen SHA for one workspace. This
        is the DETECTOR'S OWN namespace — distinct from any future
        engineer-brief bookmark, so D1/D2 advancing the detector's view of
        "what changed" can't interfere with the engineer's catch-up read."""
        self.set_meta(f"trend_bookmark:{workspace_dir}", sha)

    def get_trend_bookmark(self, workspace_dir: str) -> Optional[str]:
        """The trend detector's last-seen SHA for one workspace, or ``None``
        if unset (first observation — bookmark-aware signals seed-and-skip)."""
        return self.get_meta(f"trend_bookmark:{workspace_dir}")
