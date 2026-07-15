"""Self-observability — the deduplicated problems catalog (the ACCUMULATION
layer: capture + dedup + count).

The blindness problem this closes: devclaw hits failures all day — a goal
blocks, a task settles ``failed``, a usage limit pauses the account, a cognition
call crashes — and today each one is a line in a log that scrolls away. "It
fails/stalls N times a day" is unanswerable because nothing counts the DISTINCT
root causes.

:class:`ProblemsMixin` gives :class:`~devclaw.state_store.StateStore` one method,
:meth:`ProblemsMixin.record_problem`, wired at every failure choke point. It is
pure mechanism — **no LLM, no subprocess** — so it is safe to call on any real
failure path without touching the zero-token idle guard (it fires only when a
failure actually happened, never on an idle/no-error tick).

The crux is DEDUP: two occurrences of the same root cause must collapse into ONE
row with ``count`` incremented, not two rows — otherwise the table grows without
bound (the #250 lesson). :func:`normalize` strips the variable bits of a failure
message (uuids, paths, goal/task ids, numbers, timestamps) so "lost ref for
program 3f9a…" and "lost ref for program 88bc…" fingerprint identically. The
fingerprint is ``category | kind | normalize(message)`` and the write is an
UPSERT keyed on it.

This is the CAPTURE layer only. A ranked report over the table, and any
dreaming / auto-approval on top of it, are deliberate follow-ups — not here.
"""

from __future__ import annotations

import re
from typing import Optional

from .rows import _now_ms

# ---- normalize() — the fingerprint crux ------------------------------------
# Each regex strips one class of "variable bit" so the same ROOT CAUSE collapses
# to one fingerprint. Order matters: structural forms (paths, uuids, timestamps)
# are replaced before the catch-all bare-number pass, so a number embedded in a
# uuid/path/timestamp doesn't get half-replaced first.

#: canonical uuid4 (task/program ids are ``str(uuid.uuid4())``).
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)
#: ISO-8601 timestamps ("2026-07-15T10:30:00.123Z", "2026-07-15 10:30").
_ISO_TS = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?z?\b", re.I
)
#: wall-clock times ("10:30pm", "12:20am", "3:30").
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", re.I)
#: absolute paths — at least two ``/segment`` pieces so a lone "/" or a URL
#: scheme isn't swallowed. Runs before uuid so a path CONTAINING one collapses
#: whole (the path placeholder wins over its inner id).
_PATH = re.compile(r"(?:/[\w.\-]+){2,}/?")
#: hex blobs / short shas (7+ hex chars) not already caught as a uuid.
_HEX = re.compile(r"\b[0-9a-f]{7,}\b", re.I)
#: any remaining bare number (counts, ports, byte sizes, "500", "3600s").
_NUM = re.compile(r"\d+")
_WS = re.compile(r"\s+")

#: fingerprint/summary length cap — long enough to keep two genuinely different
#: messages apart, short enough that the row stays small.
NORMALIZE_MAX_LEN = 200


def normalize(message: Optional[str], *, max_len: int = NORMALIZE_MAX_LEN) -> str:
    """Collapse the variable bits of a failure ``message`` to a stable summary
    used for the dedup fingerprint. Lowercases, replaces uuids / absolute paths
    / goal-task ids / numbers / timestamps with placeholders, collapses
    whitespace, and truncates. Two messages that differ ONLY in such a variable
    bit return the SAME string; genuinely different messages return different
    strings. Kept deliberately small + pure so it is unit-tested directly."""
    s = (message or "").strip().lower()
    s = _PATH.sub("<path>", s)
    s = _UUID.sub("<id>", s)
    s = _ISO_TS.sub("<ts>", s)
    s = _CLOCK.sub("<ts>", s)
    s = _HEX.sub("<hex>", s)
    s = _NUM.sub("<n>", s)
    s = _WS.sub(" ", s).strip()
    return s[:max_len]


#: the fixed category vocabulary (the schema's ``category`` column). "other" is
#: the fallback so a mis-typed category can never lose a problem.
PROBLEM_CATEGORIES = (
    "block",
    "task_fail",
    "gate",
    "delivery",
    "limit",
    "cognition",
    "subprocess",
    "other",
)


class ProblemsMixin:
    """The single writer to the ``problems`` table. Lives on the SAME
    :class:`~devclaw.state_store.StateStore` instance as every other writer, so
    the single-connection / single-writer / lock semantics are identical — no
    second connection races the task/goal writers.

    ADDING A FUTURE FAILURE SITE is one line: ``store.record_problem(
    category=..., kind=..., message=..., recovered=<bool>)``. That's the whole
    integration surface — the dedup/normalize/upsert is all here."""

    def record_problem(
        self,
        *,
        category: str,
        kind: str,
        message: str,
        recovered: bool,
        goal_id: str = "",
        task_id: str = "",
    ) -> None:
        """Record ONE occurrence of a failure, deduplicated by fingerprint.

        The same root cause seen again UPSERTs the existing row (``count += 1``,
        the recovered/terminal counter bumped, last-seen refreshed) rather than
        appending a new row — so the table holds distinct PROBLEMS, not
        occurrences, and stays bounded (the #250 lesson).

        ``recovered=True`` means devclaw carried on past this failure (a usage
        limit that auto-resumes, a mechanical block that self-heals) — it bumps
        ``recovered_count``. ``recovered=False`` (a terminal task failure, a
        human-gated block) bumps ``terminal_count``. Both always bump ``count``.

        Best-effort like the trace recorders: any hiccup here is swallowed — a
        problem-recording failure must NEVER fail the operation it observes."""
        try:
            cat = (category or "other").strip()[:32] or "other"
            k = (kind or "").strip().replace("\n", " ")[:120]
            summary = normalize(message)
            fingerprint = f"{cat}|{k}|{summary}"
            sample = (message or "").strip().replace("\n", " ")[:500]
            now = _now_ms()
            rec = 1 if recovered else 0
            term = 0 if recovered else 1
            with self._lock:
                self._db.execute(
                    """INSERT INTO problems
                         (fingerprint, category, kind, summary, sample_message,
                          count, recovered_count, terminal_count,
                          first_seen_ms, last_seen_ms, last_goal_id, last_task_id)
                       VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(fingerprint) DO UPDATE SET
                         count = count + 1,
                         recovered_count = recovered_count + ?,
                         terminal_count = terminal_count + ?,
                         last_seen_ms = ?,
                         last_goal_id = ?,
                         last_task_id = ?,
                         sample_message = ?""",
                    (
                        fingerprint, cat, k, summary, sample,
                        rec, term, now, now, goal_id or "", task_id or "",
                        # ON CONFLICT bind params:
                        rec, term, now, goal_id or "", task_id or "", sample,
                    ),
                )
                self._commit()
        except Exception:
            # Telemetry must never break the observed operation. Swallow.
            pass

    def list_problems(
        self,
        *,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Distinct problems, most-frequent first (rides ``idx_problems_count``).
        The read side the future ranked report is built on; also the assertion
        surface for the dedup/bounded tests. Pure SELECT — no LLM."""
        sql = (
            "SELECT fingerprint, category, kind, summary, sample_message, count, "
            "recovered_count, terminal_count, first_seen_ms, last_seen_ms, "
            "last_goal_id, last_task_id FROM problems"
        )
        args: list[object] = []
        if category:
            sql += " WHERE category = ?"
            args.append(category)
        sql += " ORDER BY count DESC, last_seen_ms DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def count_problems(self) -> int:
        """Number of DISTINCT problems (rows) in the catalog. The bounded-table
        assertion: N occurrences of one problem must keep this at 1."""
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) AS n FROM problems").fetchone()
        return int(row["n"])
