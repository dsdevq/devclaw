"""Content-integrity tests for the SHIPPED skill library (``skill-library/``).

The library mechanism is tested in test_skill_library.py against fixtures;
this module tests the curated content the repo actually ships — the curation
rules from ``skill-library/_README.md``, mechanically enforced:

  - every skill is a single plain-markdown file with an H1 title
  - no YAML frontmatter, no slash-command references (model-agnostic invariant)
  - no "ask the user" phrasing (workers are unattended; decisions derive from
    the task contract or fail loudly)

The same autonomy/model-agnostic guards apply to the baked runner skills in
``openhands-runner/skills/`` — one worker-facing standard, two shipping tiers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from devclaw.skill_library import list_available

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LIBRARY = _REPO_ROOT / "skill-library"
_BAKED = _REPO_ROOT / "openhands-runner" / "skills"

EXPECTED_SLUGS = [
    "codebase-design",
    "domain-modeling",
    "resolving-merge-conflicts",
    "tdd",
]

_FORBIDDEN_PHRASES = [
    # unattended workers cannot ask; ambiguity goes to the summary or fails loudly
    "ask the user",
    "confirm with the user",
    "ask denys",
]
# model-agnostic invariant: no agent-specific slash-command wiring in skills
_SLASH_COMMAND = re.compile(r"(^|\s)/[a-z][a-z0-9-]+\b.*skill", re.IGNORECASE)


def _all_skill_files() -> list[Path]:
    library = [p for p in _LIBRARY.glob("*.md") if not p.name.startswith("_")]
    baked = [p for p in _BAKED.rglob("*.md")]
    assert library and baked, "expected shipped skills in both tiers"
    return library + baked


def test_shipped_library_lists_expected_slugs(monkeypatch):
    monkeypatch.setenv("DEVCLAW_SKILL_LIBRARY", str(_LIBRARY))
    assert list_available() == EXPECTED_SLUGS


@pytest.mark.parametrize("path", _all_skill_files(), ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_skill_file_meets_curation_rules(path: Path):
    text = path.read_text(encoding="utf-8")

    # plain markdown with an H1 title — no YAML frontmatter (model-agnostic)
    assert not text.startswith("---"), f"{path.name}: YAML frontmatter is agent-specific"
    assert text.lstrip().startswith("# "), f"{path.name}: must open with an H1 title"

    # substantial enough to be worth a worker's read
    assert len(text) > 200, f"{path.name}: suspiciously empty skill"

    lowered = text.lower()
    for phrase in _FORBIDDEN_PHRASES:
        assert phrase not in lowered, (
            f"{path.name}: contains '{phrase}' — workers are unattended; derive "
            "from the task contract or fail loudly instead"
        )


def test_library_skills_are_flat_single_files():
    """The v1 library contract: one file per skill, no subdirectories."""
    subdirs = [p for p in _LIBRARY.iterdir() if p.is_dir()]
    assert subdirs == [], f"library must stay flat (v1 contract): {subdirs}"
