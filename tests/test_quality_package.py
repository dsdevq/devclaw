"""The quality gate is a self-contained package — boundary pinned.

Everything the gate needs to render a verdict (logic, prompts, loader) lives
under devclaw/quality/; its only internal deps are deliberate leaf modules
(llm_call, model_tiers, loom, cognition's bind seam). These pin the boundary
so a future extraction to its own repo stays a directory move, and so the
prompt relocation (devclaw/prompts/ → devclaw/quality/prompts/, 2026-07-19)
can't silently regress.
"""

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def test_quality_imports_without_planner_queue_goal_or_state_store():
    # Fresh interpreter: the gate must not pull the heavy modules at import.
    code = (
        "import sys; import devclaw.quality; "
        "import devclaw.quality.browser_gate, devclaw.quality.reachability, "
        "devclaw.quality.eval_judge, devclaw.quality.evals; "
        "heavy = [m for m in ('devclaw.planner', 'devclaw.task_queue', "
        "'devclaw.goal', 'devclaw.state_store', 'devclaw.task_git') "
        "if m in sys.modules]; "
        "assert not heavy, f'gate pulled heavy modules: {heavy}'; print('gate-ok')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=_REPO
    )
    assert out.returncode == 0, out.stderr
    assert "gate-ok" in out.stdout


def test_gate_prompts_live_inside_the_package():
    pkg = _REPO / "devclaw" / "quality" / "prompts"
    for slug in ("review-gate", "eval-judge", "browser-reachability"):
        assert (pkg / f"{slug}.md").is_file(), f"{slug}.md missing from the package"
        # and they are GONE from the devclaw-wide prompt dir — one home only
        assert not (_REPO / "devclaw" / "prompts" / f"{slug}.md").exists(), (
            f"{slug}.md still (or again) in devclaw/prompts/ — two homes drift"
        )


def test_package_loader_renders_the_gate_prompts():
    from devclaw.quality.prompts import load_prompt

    review = load_prompt("review-gate")
    # spot-check load-bearing content survived the move: the #227 grounding
    # clause and the JSON-verdict contract ({{ }} unescaped to { })
    assert "REPOSITORY CONTEXT" in review or "Repository context" in review
    assert "{" in review
    for slug in ("eval-judge", "browser-reachability"):
        assert load_prompt(slug).strip()


def test_package_loader_matches_devclaw_loader_semantics():
    # same contract as devclaw.prompts.load_prompt: unknown slug raises
    # FileNotFoundError (not a silent empty prompt)
    import pytest

    from devclaw.quality.prompts import load_prompt

    with pytest.raises(FileNotFoundError):
        load_prompt("no-such-prompt")
