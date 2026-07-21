"""Greenfield toolchain-declaration nudge (ADR 0005, PR 3).

The sandbox provisions the toolchain from the repository's OWN declaration
files (mise-native, or translated global.json / package.json engines) — so a
greenfield goal whose repo declares nothing would dispatch onto the bare
python+node base image and fail at the first `dotnet`/versioned-node step.
The fix lives in cognition: the decomposer must plan the declaration file as
the FIRST checklist item (stack items depending on it), and firming must
surface the missing declaration as a blocker so the decomposer sees it.

Per rules/testing.md, prompt-content tests assert presence AND the guard —
the clause must also forbid emitting the item/blocker when a declaration
already exists (the conditional is what keeps the nudge grounded, not a
canned prior). Raw-template assertions come first so the rendered
assertions aren't vacuous.
"""

from __future__ import annotations

from pathlib import Path

from devclaw.prompts import load_prompt

_PROMPTS = Path(__file__).resolve().parents[1] / "devclaw" / "prompts"


def _raw(slug: str) -> str:
    return (_PROMPTS / f"{slug}.md").read_text(encoding="utf-8")


# ---- decomposer ----


def test_decomposer_template_carries_the_greenfield_declaration_step():
    raw = _raw("decomposer")
    # the rule exists, names the declaration files, and demands FIRST-item order
    assert "DECLARE their toolchain first" in raw
    assert ".mise.toml" in raw and ".tool-versions" in raw
    assert "global.json" in raw and "engines" in raw
    assert "FIRST\nchecklist item" in raw or "FIRST checklist item" in raw.replace("\n", " ")
    # the guard: never emit the item when a declaration already exists —
    # grounded in the digest/context, not a canned default
    assert "Do NOT emit this item when a\ndeclaration already exists" in raw or (
        "Do NOT emit this item" in raw and "declaration already exists" in raw
    )


def test_decomposer_rendered_prompt_carries_the_step():
    rendered = load_prompt("decomposer")
    assert "DECLARE their toolchain first" in rendered
    assert "depends_on` it (directly or transitively)" in rendered.replace("``", "`")


# ---- firming ----


def _render_firming() -> str:
    return load_prompt(
        "firming",
        objective="obj",
        done_when="dw",
        verify_cmd="(not specified)",
        round=1,
        spec="(no spec)",
        discovery_brief="(no discovery brief yet)",
        repo_context_block="",
        prior_draft="(none)",
        owner_answers="(none)",
    )


def test_firming_template_carries_the_toolchain_blocker_rule():
    raw = _raw("firming")
    assert "missing toolchain declaration is a blocker" in raw
    assert ".mise.toml" in raw and "package.json" in raw
    # routed through the existing blockers[] channel, not a new output field
    assert "add a `blockers[]`\nline" in raw or (
        "blockers[]" in raw and "the decomposer turns\nit into the first checklist item" in raw
    )
    # the guard against ungrounded emission
    assert "Do NOT add the line when a\ndeclaration already exists" in raw or (
        "Do NOT add the line" in raw and "declaration already exists" in raw
    )


def test_firming_rendered_prompt_carries_the_rule():
    rendered = _render_firming()
    assert "missing toolchain declaration is a blocker" in rendered
    assert "ground its absence" in rendered
