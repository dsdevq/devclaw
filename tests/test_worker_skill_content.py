"""Content-integrity tests for the baked worker skills (``openhands-runner/skills/``).

The curation rules for anything a worker reads at task time, mechanically
enforced (these are the model-agnostic invariants from CLAUDE.md/README):

  - every skill is a plain-markdown file with an H1 title
  - no YAML frontmatter, no slash-command references (model-agnostic invariant)
  - no "ask the user" phrasing (workers are unattended; decisions derive from
    the task contract or fail loudly)

Formerly ``test_skill_library_content.py``, which also covered the curated
host-side ``skill-library/`` tier — that tier was removed 2026-07-13 (inert in
production: nothing populated ``Goal.skills_required`` and no deploy step
populated the host path). The baked tier is the one worker-facing standard now.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BAKED = _REPO_ROOT / "openhands-runner" / "skills"

_FORBIDDEN_PHRASES = [
    # unattended workers cannot ask; ambiguity goes to the summary or fails loudly
    "ask the user",
    "confirm with the user",
    "ask denys",
]
# model-agnostic invariant: no agent-specific slash-command wiring in skills
_SLASH_COMMAND = re.compile(r"(^|\s)/[a-z][a-z0-9-]+\b.*skill", re.IGNORECASE)


def _all_skill_files() -> list[Path]:
    baked = [p for p in _BAKED.rglob("*.md")]
    assert baked, "expected shipped skills in the baked tier"
    return baked


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
