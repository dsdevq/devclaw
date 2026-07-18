"""The self-triage prompt template — grounding discipline + no leaked headers.

Mirrors the cognition-prompts rules (rules/cognition-prompts.md): a repo-reasoning
prompt must carry the #227 grounding clause, and must NOT quote a ``## header``
inside its instruction text (that leaks the literal header into every rendering
and makes omission tests vacuous, per #234).
"""

from __future__ import annotations

from devclaw.goal import triage as _triage
from devclaw.prompts import load_prompt


def _norm(raw: str) -> str:
    """Collapse whitespace (markdown line-wraps a clause across newlines) so a
    substring assertion tests the PROSE, not the wrapping."""
    return " ".join(raw.lower().split())


def test_prompt_carries_the_227_grounding_clause():
    low = _norm(load_prompt("self-triage", problem="p", catalog="c", repo_context="r"))
    assert "do not infer" in low
    # the "absent ⇒ unknown" discipline, phrased as prose
    assert "absent" in low and "unknown" in low
    assert "working directory" in low        # names the exact leak the clause forbids


def test_prompt_is_propose_only():
    low = _norm(load_prompt("self-triage", problem="p", catalog="c", repo_context="r"))
    assert "propose only" in low or "propose-only" in low
    assert "never claim to have" in low


def test_prompt_does_not_leak_a_markdown_header_in_instructions():
    """The grounding clause references `Repository context` WITHOUT the leading
    `##` — the section header appears once (as a real header), never quoted in
    the instruction body."""
    raw = load_prompt("self-triage", problem="p", catalog="c", repo_context="r")
    # exactly one real header line for the grounded-facts section
    header_lines = [ln for ln in raw.splitlines() if ln.strip().startswith("## Repository context")]
    assert len(header_lines) == 1
    # and the phrase "Repository context" is not quoted with ## inside prose
    assert "`## Repository context`" not in raw


def test_prompt_renders_with_the_caller_kwargs():
    """build_prompt supplies exactly problem/catalog/repo_context — a missing
    placeholder would raise at str.format, so this pins the call contract."""
    p = _triage.build_prompt("the problem", "the catalog", "the context")
    assert "the problem" in p and "the catalog" in p and "the context" in p


def test_build_prompt_blank_inputs_get_safe_placeholders():
    p = _triage.build_prompt("", "", "")
    assert "(no message)" in p
    assert "catalog empty" in p
    assert "no repository context" in p
