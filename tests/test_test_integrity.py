"""test_integrity.scan_diff — catches an agent weakening tests to fake a green gate."""
from __future__ import annotations

from devclaw.test_integrity import scan_diff


def _diff(path: str, removed: list[str] = (), added: list[str] = ()) -> str:
    lines = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", "@@ -1,5 +1,5 @@"]
    lines += [f"-{l}" for l in removed]
    lines += [f"+{l}" for l in added]
    return "\n".join(lines) + "\n"


def test_clean_added_test_is_ok():
    r = scan_diff(_diff("tests/test_api.py", added=["def test_new_case():", "    assert f() == 1"]))
    assert r.ok and r.removed_tests == 0 and r.added_skips == 0


def test_removed_test_function_flagged():
    r = scan_diff(_diff("tests/test_api.py", removed=["def test_edge_case():", "    assert g() == 2"]))
    assert not r.ok and r.removed_tests == 1
    assert "removed" in r.summary()


def test_added_skip_marker_flagged():
    r = scan_diff(_diff("tests/test_api.py", added=["@pytest.mark.skip(reason='flaky')", "def test_x():"]))
    assert not r.ok and r.added_skips == 1


def test_rename_is_net_zero():
    # moving a test (remove here, add there) must not be flagged as removal
    d = _diff("tests/test_a.py", removed=["def test_thing():"]) + _diff(
        "tests/test_b.py", added=["def test_thing():"]
    )
    assert scan_diff(d).removed_tests == 0


def test_non_test_file_ignored():
    # a "def test_connection" in app code is not a test file → ignored
    r = scan_diff(_diff("backend/main.py", removed=["def test_connection():"]))
    assert r.ok and r.removed_tests == 0


def test_js_skip_detected():
    r = scan_diff(_diff("src/__tests__/app.test.ts", added=["  it.skip('does x', () => {})"]))
    assert not r.ok and r.added_skips == 1


def test_go_skip_detected():
    r = scan_diff(_diff("api/handler_test.go", added=["    t.Skip(\"todo\")"]))
    assert not r.ok and r.added_skips == 1
