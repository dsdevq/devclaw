"""Regression tests for the main-branch PreToolUse guard.

Named after the behavior that was broken: the guard resolved the branch from
the session cwd (the main checkout, always on `main`), so it blocked every
legitimate commit made inside a worktree the command `cd`s into — a systematic
false positive that trained the DEVCLAW_ALLOW_MAIN reflex and hollowed out the
guard. The fix resolves the dir the git command actually runs in.
"""
import importlib.util
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "main-branch-guard.py"


def _load():
    spec = importlib.util.spec_from_file_location("main_branch_guard", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load()

MAIN_CHECKOUT = "/home/x/projects/devclaw"  # the session cwd — always on main


# --- effective_cwd: which dir does the git command actually target? ---

def test_effective_cwd_variable_cd_is_unresolvable():
    # `cd "$WT" && git commit` — the worktree path is a shell var we can't
    # resolve in the hook → None (branch unknown), NOT the main checkout.
    assert guard.effective_cwd('cd "$WT" && git commit -m x', MAIN_CHECKOUT) is None


def test_effective_cwd_literal_cd_wins_over_payload():
    assert guard.effective_cwd("cd /tmp/wt && git commit -m x", MAIN_CHECKOUT) == "/tmp/wt"


def test_effective_cwd_git_dash_C_dir_form():
    assert guard.effective_cwd("git -C /tmp/wt commit -m x", MAIN_CHECKOUT) == "/tmp/wt"


def test_effective_cwd_commit_dash_C_ref_is_not_a_dir():
    # `git commit -C HEAD` reuses a commit message; -C here is NOT the dir flag.
    assert guard.effective_cwd("git commit -C HEAD", MAIN_CHECKOUT) == MAIN_CHECKOUT


def test_effective_cwd_no_cd_falls_back_to_payload():
    assert guard.effective_cwd("git commit -m x", MAIN_CHECKOUT) == MAIN_CHECKOUT


# --- blocks: end-to-end behavior with a stubbed branch lookup ---

@pytest.fixture
def on_branch(monkeypatch):
    """Stub current_branch to a per-path mapping (default: main checkout→main)."""
    def _set(mapping):
        monkeypatch.setattr(guard, "current_branch",
                            lambda cwd: mapping.get(cwd, ""))
    _set({MAIN_CHECKOUT: "main"})
    return _set


def test_worktree_commit_not_blocked_the_fixed_false_positive(on_branch):
    # THE regression: a worktree commit no longer blocks just because the
    # session cwd is the main checkout.
    assert guard.blocks('cd "$WT" && git commit -m x', MAIN_CHECKOUT) is None


def test_bare_commit_in_main_checkout_still_blocked(on_branch):
    # The guard still does its job when git really runs in a main-branch dir.
    msg = guard.blocks("git commit -m x", MAIN_CHECKOUT)
    assert msg and "on main" in msg


def test_literal_cd_into_main_checkout_is_blocked(on_branch):
    # Stronger than before: a `cd` into a literal main-branch dir is caught even
    # when the session cwd is elsewhere.
    on_branch({"/repo/main": "main"})
    msg = guard.blocks("cd /repo/main && git commit -m x", "/some/worktree")
    assert msg and "on main" in msg


def test_push_targeting_main_blocked_even_when_dir_unknown(on_branch):
    # The push-targets-main check is dir-independent — fires even with a $VAR cd.
    msg = guard.blocks('cd "$WT" && git push origin HEAD:main', MAIN_CHECKOUT)
    assert msg and "main" in msg


def test_override_always_wins(on_branch):
    assert guard.blocks("DEVCLAW_ALLOW_MAIN=1 git commit -m x", MAIN_CHECKOUT) is None


def test_worktree_push_feature_branch_allowed(on_branch):
    assert guard.blocks("cd /tmp/wt && git push origin HEAD:feat/x", MAIN_CHECKOUT) is None
