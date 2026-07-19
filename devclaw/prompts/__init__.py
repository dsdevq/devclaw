"""The prompts package — markdown prompt templates, one file per role.

Why disk and not Python strings: prompts are prose the model reads, not code.
Editing one shouldn't require touching Python (and the review/CI overhead that
brings); diffing one across versions shouldn't be lost in the Python noise.
Keeping them as ``.md`` files makes prompt changes a one-file, prose-only
review.

Layout: ``devclaw/prompts/<slug>.md``. Each ``.md`` file is a complete prompt
(system role + structural constraints), interpolated at call time via
``str.format`` with whatever ``**vars`` the caller passes. Composite prompts
(grill = rules + spec-shape + transcript + closing + contract) stay assembled
in Python because the dynamic parts (transcript turns, finalize bit) need
real code — but the static blocks they assemble live here.

Use:

    from devclaw.prompts import load_prompt
    prompt = load_prompt("decomposer")
    prompt = load_prompt("research-discovery",
                         objective="ship a CLI",
                         done_when="ship it",
                         repo_analysis="…")
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def _read(slug: str) -> str:
    path = _HERE / f"{slug}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt not found: {slug} (looked at {path})")
    # rstrip — the original Python triple-strings had no trailing newline, so
    # the file's POSIX newline at EOF must be dropped to match.
    return path.read_text().rstrip()


def load_prompt(slug: str, /, **vars: object) -> str:
    """Read ``<slug>.md`` and ``str.format``-interpolate. ``{{`` / ``}}`` in the
    file become literal ``{`` / ``}`` in the output (JSON contracts), even when
    no vars are passed — the loader always runs ``.format`` so the file's
    escaping is consistent regardless of whether the caller has placeholders.

    The file is cached on first read for the process lifetime — prompts don't
    change at runtime in production. Tests that need to reload can call
    ``devclaw.prompts._read.cache_clear()``.
    """
    return _read(slug).format(**vars)
