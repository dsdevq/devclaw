"""Shared protocol reader for engine runners.

Both engines — the docker sandbox (`sandcastle_runner`) and the host
(`host_runner`) — spawn ``openhands-runner/runner.py``, which streams
``event: <json>`` lines plus one terminating ``result: <json>`` line. This
consumes that protocol off a subprocess's stdout: dispatching events to the
callback and returning the parsed result (or an error if none arrived).
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Callable, Optional

from . import EngineEvent, EngineResult

#: StreamReader line-buffer limit for runner stdout. The runner emits one JSON
#: object per line (``event:`` / ``result:``), and a single event can be large —
#: a file read/write observation, a big diff. asyncio's DEFAULT 64 KiB limit
#: crashed a real feature run ("Separator is not found, and chunk exceed the
#: limit") on an oversized event line, failing an otherwise-correct task. 64 MiB
#: is far above any real event while still bounding memory. Both engines pass this
#: to ``create_subprocess_exec(limit=...)`` so the shared reader below never trips.
STREAM_LINE_LIMIT = 64 * 1024 * 1024


async def consume_runner_output(
    proc: asyncio.subprocess.Process,
    on_event: Optional[Callable[[EngineEvent], None]],
    *,
    label: str = "runner",
) -> EngineResult:
    result: Optional[EngineResult] = None
    stderr_chunks: list[bytes] = []

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        async for line in proc.stderr:
            stderr_chunks.append(line)

    stderr_task = asyncio.ensure_future(drain_stderr())

    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        if line.startswith("event: "):
            if on_event:
                try:
                    data = json.loads(line[len("event: ") :])
                    on_event(
                        EngineEvent(
                            id=data.get("id"),
                            type=data.get("type", ""),
                            source=data.get("source", ""),
                            ts=data.get("ts", 0),
                            payload=data.get("payload"),
                        )
                    )
                except json.JSONDecodeError as parse_err:
                    sys.stderr.write(f"{label}: dropping malformed event line: {parse_err}\n")
        elif line.startswith("result: "):
            if result is None:  # first result line wins; ignore anything after
                try:
                    result = json.loads(line[len("result: ") :])
                except json.JSONDecodeError as parse_err:
                    result = {
                        "status": "error",
                        "error": f"runner emitted unparsable result: {parse_err}",
                        "trace": line,
                    }
        # everything else is decorative runner output — drop

    await proc.wait()
    stderr_task.cancel()
    stderr_text = b"".join(stderr_chunks).decode("utf-8", "replace")
    if result is not None:
        return result
    return {
        "status": "error",
        "error": (
            f"{label} exited {proc.returncode} without a result line. "
            f"stderr tail:\n{stderr_text[-1024:]}"
        ),
    }
