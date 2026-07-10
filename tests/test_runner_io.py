"""Runner-IO reader tests — large event lines must not crash the task.

A real feature run failed with "Separator is not found, and chunk exceed the
limit": the runner emitted an `event:` line bigger than asyncio's default 64 KiB
StreamReader buffer, and the read crashed an otherwise-correct task. The engines
now create the subprocess with STREAM_LINE_LIMIT; these prove the shared reader
handles an oversized line under that limit (and document the default crash).
"""

import asyncio
import json

import pytest

from devclaw.engine.runner_io import STREAM_LINE_LIMIT, consume_runner_output


class _FakeProc:
    def __init__(self, stdout, stderr):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = 0

    async def wait(self):
        return self.returncode


def _fed_reader(data: bytes, limit: int) -> asyncio.StreamReader:
    r = asyncio.StreamReader(limit=limit)
    r.feed_data(data)
    r.feed_eof()
    return r


def test_stream_line_limit_is_generous():
    # well above any real event (64 KiB default was the bug), still bounded.
    assert STREAM_LINE_LIMIT >= 16 * 1024 * 1024


async def test_large_event_line_is_handled_under_the_limit():
    big = "x" * (256 * 1024)  # 256 KiB — 4x over the old 64 KiB default
    lines = (
        "event: " + json.dumps({"id": "1", "type": "ObservationEvent",
                                 "source": "agent", "ts": 0, "payload": {"blob": big}}) + "\n"
        + "result: " + json.dumps({"status": "ok", "message": "done"}) + "\n"
    ).encode()
    seen: list[str] = []
    proc = _FakeProc(_fed_reader(lines, STREAM_LINE_LIMIT),
                     _fed_reader(b"", STREAM_LINE_LIMIT))
    result = await consume_runner_output(proc, lambda e: seen.append(e.type))
    assert result == {"status": "ok", "message": "done"}
    assert seen == ["ObservationEvent"]  # the big event was delivered, not dropped


async def test_default_64k_limit_would_crash_on_the_same_line():
    # documents the bug: under asyncio's default limit, the oversized line throws.
    big = "x" * (256 * 1024)
    line = ("event: " + json.dumps({"payload": big}) + "\n").encode()
    proc = _FakeProc(_fed_reader(line, 64 * 1024), _fed_reader(b"", 64 * 1024))
    with pytest.raises(ValueError):
        await consume_runner_output(proc, None)


async def test_rate_limited_result_line_passes_through_unchanged():
    # the runner's structured usage-limit signal (status="rate_limited") must
    # reach the task queue verbatim — the reader parses JSON as-is and never
    # filters on status values.
    payload = {
        "status": "rate_limited",
        "error": "You've hit your session limit · resets 12:20am",
        "retry_after": None,
    }
    lines = ("result: " + json.dumps(payload) + "\n").encode()
    proc = _FakeProc(_fed_reader(lines, STREAM_LINE_LIMIT),
                     _fed_reader(b"", STREAM_LINE_LIMIT))
    result = await consume_runner_output(proc, None)
    assert result == payload
