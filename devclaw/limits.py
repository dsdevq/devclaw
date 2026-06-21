"""Back-compat shim. The failure classifier moved to :mod:`devclaw.loom.limits`
during the ``loom`` extraction (the reusable orchestration core, dropping the
``-claw`` prefix). Import from :mod:`devclaw.loom` going forward; this module
re-exports the same names so existing imports keep working."""

from __future__ import annotations

from .loom.limits import (  # noqa: F401
    RATE_LIMIT_MAX_PAUSE_S,
    RATE_LIMIT_PAUSE_S,
    Classification,
    FailureKind,
    PAUSING_KINDS,
    _parse_retry_after,
    classify_failure,
    pause_seconds,
)
