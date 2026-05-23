"""Tests for orchestrator.atomic_check.is_truly_atomic.

Covers each of the 8 atomic-merge rules with a positive case (qualifies)
and a negative case (correctly disqualified), plus a synthetic ci-fix PR
scenario that should qualify, plus the ci_failure_dispatcher author shape
matching what the dispatcher actually opens PRs as.
"""

from __future__ import annotations

from orchestrator.atomic_check import is_truly_atomic, load_rules


def _baseline_metadata(**overrides):
    """A minimal PR that satisfies every rule. Tests start from here and
    mutate one field to exercise a specific rule."""
    md = {
        "author": "ci_failure_dispatcher",
        "files": [
            {"path": "orchestrator/src/orchestrator/foo.py", "additions": 10},
        ],
    }
    md.update(overrides)
    return md


# ─── Sanity ─────────────────────────────────────────────────────────────────


def test_baseline_qualifies() -> None:
    ok, reason = is_truly_atomic(_baseline_metadata())
    assert ok, reason
    assert reason == ""


def test_rules_yaml_loads() -> None:
    rules = load_rules()
    # Sanity-check that the shipped config has every keyed list/threshold.
    assert rules["max_files_changed"] == 3
    assert rules["max_added_lines"] == 150
    for key in (
        "public_api_patterns",
        "schema_patterns",
        "dep_patterns",
        "ci_infra_patterns",
        "security_patterns",
        "test_patterns",
        "allowlisted_dispatchers",
    ):
        assert isinstance(rules[key], list) and rules[key], f"{key} should be a non-empty list"


# ─── Rule 1 — File count ────────────────────────────────────────────────────


def test_rule1_positive_three_files_qualifies() -> None:
    md = _baseline_metadata(files=[
        {"path": "a.py", "additions": 5},
        {"path": "b.py", "additions": 5},
        {"path": "c.py", "additions": 5},
    ])
    ok, _ = is_truly_atomic(md)
    assert ok


def test_rule1_negative_four_files_disqualifies() -> None:
    md = _baseline_metadata(files=[
        {"path": "a.py", "additions": 1},
        {"path": "b.py", "additions": 1},
        {"path": "c.py", "additions": 1},
        {"path": "d.py", "additions": 1},
    ])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 1" in reason
    assert "4 files" in reason


# ─── Rule 2 — Public API ────────────────────────────────────────────────────


def test_rule2_positive_internal_change_qualifies() -> None:
    md = _baseline_metadata(files=[{"path": "src/internal/helper.py", "additions": 5}])
    ok, _ = is_truly_atomic(md)
    assert ok


def test_rule2_negative_api_path_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": "src/api/v1/handler.py", "additions": 5}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 2" in reason


def test_rule2_negative_init_py_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": "pkg/__init__.py", "additions": 3}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 2" in reason


def test_rule2_negative_proto_file_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": "proto/messages.proto", "additions": 2}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 2" in reason


# ─── Rule 3 — Schema ────────────────────────────────────────────────────────


def test_rule3_positive_non_schema_file_qualifies() -> None:
    md = _baseline_metadata(files=[{"path": "src/utils.py", "additions": 5}])
    ok, _ = is_truly_atomic(md)
    assert ok


def test_rule3_negative_migration_disqualifies() -> None:
    md = _baseline_metadata(files=[
        {"path": "app/migrations/0042_add_col.py", "additions": 15},
    ])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 3" in reason


def test_rule3_negative_sql_file_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": "db/seeds/seed.sql", "additions": 5}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 3" in reason


# ─── Rule 4 — Dependencies ──────────────────────────────────────────────────


def test_rule4_positive_no_manifest_qualifies() -> None:
    md = _baseline_metadata(files=[{"path": "src/handler.ts", "additions": 5}])
    ok, _ = is_truly_atomic(md)
    assert ok


def test_rule4_negative_package_json_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": "package.json", "additions": 2}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 4" in reason


def test_rule4_negative_nested_lockfile_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": "frontend/package-lock.json", "additions": 8}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 4" in reason


def test_rule4_negative_pyproject_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": "orchestrator/pyproject.toml", "additions": 1}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 4" in reason


# ─── Rule 5 — CI / infra ────────────────────────────────────────────────────


def test_rule5_positive_no_ci_qualifies() -> None:
    md = _baseline_metadata(files=[{"path": "src/handler.py", "additions": 3}])
    ok, _ = is_truly_atomic(md)
    assert ok


def test_rule5_negative_workflow_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": ".github/workflows/ci.yml", "additions": 4}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 5" in reason


def test_rule5_negative_dockerfile_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": "Dockerfile.web", "additions": 1}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 5" in reason


def test_rule5_negative_makefile_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": "infra/Makefile", "additions": 5}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 5" in reason


# ─── Rule 6 — Security-sensitive ────────────────────────────────────────────


def test_rule6_positive_non_security_qualifies() -> None:
    md = _baseline_metadata(files=[{"path": "src/feature.py", "additions": 5}])
    ok, _ = is_truly_atomic(md)
    assert ok


def test_rule6_negative_auth_path_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": "app/auth/login.py", "additions": 5}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 6" in reason


def test_rule6_negative_env_file_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": "deploy/.env.production", "additions": 2}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 6" in reason


def test_rule6_negative_pem_file_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": "certs/server.pem", "additions": 30}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 6" in reason


def test_rule6_negative_middleware_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": "src/middleware/cors.py", "additions": 3}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 6" in reason


# ─── Rule 7 — Diff size ─────────────────────────────────────────────────────


def test_rule7_positive_small_diff_qualifies() -> None:
    md = _baseline_metadata(files=[{"path": "src/handler.py", "additions": 149}])
    ok, _ = is_truly_atomic(md)
    assert ok


def test_rule7_negative_large_diff_disqualifies() -> None:
    md = _baseline_metadata(files=[{"path": "src/handler.py", "additions": 151}])
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 7" in reason


def test_rule7_positive_large_test_diff_qualifies() -> None:
    """Test files don't count toward the line cap — heavy unit tests for a
    small fix should still be auto-mergeable."""
    md = _baseline_metadata(files=[
        {"path": "src/handler.py", "additions": 20},
        {"path": "tests/test_handler.py", "additions": 800},
    ])
    ok, _ = is_truly_atomic(md)
    assert ok


def test_rule7_positive_test_file_naming_excluded() -> None:
    md = _baseline_metadata(files=[
        {"path": "src/utils.py", "additions": 10},
        {"path": "src/utils_test.py", "additions": 500},
        {"path": "src/utils.test.ts", "additions": 400},
    ])
    ok, _ = is_truly_atomic(md)
    assert ok


# ─── Rule 8 — Source allowlist ──────────────────────────────────────────────


def test_rule8_positive_dispatcher_author_qualifies() -> None:
    md = _baseline_metadata(author="ci_failure_dispatcher")
    ok, _ = is_truly_atomic(md)
    assert ok


def test_rule8_positive_scout_author_qualifies() -> None:
    md = _baseline_metadata(author="scout")
    ok, _ = is_truly_atomic(md)
    assert ok


def test_rule8_negative_human_author_disqualifies() -> None:
    md = _baseline_metadata(author="a-human-developer")
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 8" in reason


def test_rule8_negative_missing_author_disqualifies() -> None:
    md = _baseline_metadata(author="")
    ok, reason = is_truly_atomic(md)
    assert not ok
    assert "rule 8" in reason


# ─── Synthetic ci-fix scenario ──────────────────────────────────────────────


def test_synthetic_ci_fix_pr_qualifies() -> None:
    """Realistic shape of a dispatcher-opened PR fixing a flaky test: one
    source file, one test update, opened by the ci_failure_dispatcher."""
    md = {
        "author": "ci_failure_dispatcher",
        "files": [
            {"path": "orchestrator/src/orchestrator/sweep.py", "additions": 12},
            {"path": "orchestrator/tests/test_sweep.py", "additions": 38},
        ],
    }
    ok, reason = is_truly_atomic(md)
    assert ok, reason


# ─── Custom rule override ───────────────────────────────────────────────────


def test_rules_param_overrides_yaml() -> None:
    """Callers can pass a `rules` dict to bypass the on-disk config — useful
    for tests and for sweeping a temporary policy change without editing
    the YAML."""
    md = _baseline_metadata(files=[
        {"path": "a.py", "additions": 1},
        {"path": "b.py", "additions": 1},
    ])
    strict = {
        "max_files_changed": 1,
        "max_added_lines": 150,
        "allowlisted_dispatchers": ["ci_failure_dispatcher"],
        "public_api_patterns": [],
        "schema_patterns": [],
        "dep_patterns": [],
        "ci_infra_patterns": [],
        "security_patterns": [],
        "test_patterns": [],
    }
    ok, reason = is_truly_atomic(md, rules=strict)
    assert not ok
    assert "rule 1" in reason


# ─── Alternate input shape (files_changed list) ─────────────────────────────


def test_files_changed_list_shape_supported() -> None:
    """If the caller has just a list of paths (no additions data), the check
    should still work — additions default to 0, file-count and pattern rules
    still apply."""
    md = {
        "author": "scout",
        "files_changed": ["src/util.py"],
    }
    ok, _ = is_truly_atomic(md)
    assert ok
