"""Claude workspace-trust seeding — the config twin of the mise trust seam.

Regression for the 2026-07 reliability collapse: Claude Code began enforcing
workspace trust, and devclaw seeded it nowhere, so its planner (`/app`) and its
workers (`/workspace`) dead-stopped on the untrusted-workspace guard — the #1
and #2 terminal-failure classes. These pin the seeding so it can't silently
regress, and prove the load-bearing safety properties: identity is preserved,
permissions are only restored (never widened), and every path is best-effort
(a config we can't touch is left alone, never masking a real failure).
"""

import json
import os

from devclaw import claude_trust as ct


# ---- apply_trust (pure) ----


def test_apply_trust_marks_workspace_and_reports_changed():
    cfg: dict = {}
    assert ct.apply_trust(cfg, "/workspace") is True
    assert cfg["projects"]["/workspace"]["hasTrustDialogAccepted"] is True


def test_apply_trust_is_idempotent():
    cfg: dict = {}
    ct.apply_trust(cfg, "/workspace")
    # Second application changes nothing — callers skip a redundant write.
    assert ct.apply_trust(cfg, "/workspace") is False


def test_apply_trust_preserves_identity_and_other_projects():
    cfg = {
        "oauthAccount": {"emailAddress": "x@y.z"},
        "userID": "abc123",
        "projects": {"/other": {"hasTrustDialogAccepted": True, "history": [1, 2]}},
    }
    ct.apply_trust(cfg, "/workspace")
    # The account identity the ACP loop needs is untouched...
    assert cfg["oauthAccount"] == {"emailAddress": "x@y.z"}
    assert cfg["userID"] == "abc123"
    # ...and an unrelated project's entry is left exactly as it was.
    assert cfg["projects"]["/other"] == {"hasTrustDialogAccepted": True, "history": [1, 2]}


def test_apply_trust_repairs_non_dict_projects():
    cfg = {"projects": "corrupt"}
    assert ct.apply_trust(cfg, "/workspace") is True
    assert cfg["projects"]["/workspace"]["hasTrustDialogAccepted"] is True


# ---- ensure_trusted_in_place (host cognition config; editable) ----


def test_ensure_trusted_in_place_writes_and_is_idempotent(tmp_path):
    cfg_path = str(tmp_path / ".claude.json")
    cfg_path_obj = tmp_path / ".claude.json"
    cfg_path_obj.write_text(json.dumps({"userID": "u", "projects": {}}))

    assert ct.ensure_trusted_in_place(cfg_path, "/app") is True
    written = json.loads(cfg_path_obj.read_text())
    assert written["projects"]["/app"]["hasTrustDialogAccepted"] is True
    assert written["userID"] == "u"  # identity preserved through the rewrite

    # Already trusted → no rewrite, returns False.
    assert ct.ensure_trusted_in_place(cfg_path, "/app") is False


def test_ensure_trusted_in_place_creates_missing_config(tmp_path):
    cfg_path = str(tmp_path / "nested" / ".claude.json")
    assert ct.ensure_trusted_in_place(cfg_path, "/app") is True
    written = json.loads((tmp_path / "nested" / ".claude.json").read_text())
    assert written["projects"]["/app"]["hasTrustDialogAccepted"] is True


def test_ensure_trusted_in_place_is_best_effort_on_unwritable(tmp_path):
    # A regular file where a directory is expected → makedirs/open raise OSError.
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    cfg_path = str(blocker / ".claude.json")
    # Never raises into cognition; just reports it couldn't seed.
    assert ct.ensure_trusted_in_place(cfg_path, "/app") is False


# ---- write_trusted_copy (sandbox config; read-only bind → copy) ----


def test_write_trusted_copy_adds_trust_and_preserves_identity(tmp_path):
    src = tmp_path / ".claude.json"
    src.write_text(json.dumps({"oauthAccount": {"e": "x"}, "userID": "u", "projects": {}}))

    out = ct.write_trusted_copy(str(src), "/workspace")
    assert out is not None
    try:
        copy = json.loads(open(out, encoding="utf-8").read())
        assert copy["projects"]["/workspace"]["hasTrustDialogAccepted"] is True
        # The identity the sandbox ACP loop needs rode along in the copy.
        assert copy["oauthAccount"] == {"e": "x"}
        assert copy["userID"] == "u"
        assert os.stat(out).st_mode & 0o004  # world-readable for the agent uid
    finally:
        os.unlink(out)


def test_write_trusted_copy_returns_none_when_source_missing(tmp_path):
    # Missing source → None so the caller falls back to the raw read-only bind
    # (pre-trust behavior), never an identity-less config that hangs the loop.
    assert ct.write_trusted_copy(str(tmp_path / "nope.json"), "/workspace") is None


def test_write_trusted_copy_returns_none_on_unparseable_source(tmp_path):
    src = tmp_path / ".claude.json"
    src.write_text("{ not json")
    assert ct.write_trusted_copy(str(src), "/workspace") is None


# ---- config_path_for (resolution matches Claude Code) ----


def test_config_path_for_uses_config_dir_when_given():
    assert ct.config_path_for("/home/agent/.claude") == "/home/agent/.claude/.claude.json"


def test_config_path_for_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert ct.config_path_for() == os.path.expanduser("~/.claude.json")
