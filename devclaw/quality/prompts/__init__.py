"""The gate's own prompt templates — inside the package boundary by design.

The three judge prompts (``review-gate.md``, ``eval-judge.md``,
``browser-reachability.md``) moved here from ``devclaw/prompts/`` (2026-07-19)
so the quality gate is self-contained: everything the gate needs to render a
verdict — logic, prompts, loader — lives under ``devclaw/quality/`` and moves
as one unit when the package is extracted to its own repo.

Same loader semantics as ``devclaw.prompts.load_prompt`` (package-relative
``<slug>.md``, cached first read, always ``.format``-interpolated so ``{{ }}``
escaping is consistent); deliberately a small local copy rather than an import
of the devclaw-wide loader — the gate must not depend on devclaw's prompt dir
existing.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def _read(slug: str) -> str:
    path = _HERE / f"{slug}.md"
    if not path.is_file():
        raise FileNotFoundError(f"gate prompt not found: {slug} (looked at {path})")
    # rstrip — match devclaw.prompts: drop the file's POSIX newline at EOF.
    return path.read_text().rstrip()


def load_prompt(slug: str, /, **vars: object) -> str:
    """Read ``<slug>.md`` and ``str.format``-interpolate. ``{{`` / ``}}`` in
    the file become literal ``{`` / ``}`` in the output (JSON contracts), even
    when no vars are passed. Cached for the process lifetime; tests that need a
    reload call ``_read.cache_clear()``."""
    return _read(slug).format(**vars)
