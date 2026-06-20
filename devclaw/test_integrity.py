"""Detect the cheapest way to fake a green gate: weakening the tests.

The verify gate only proves "the test command exited 0" — which an agent under
pressure can satisfy by **deleting a failing test, skipping it, or gutting its
assertions** rather than fixing the code. The prompt forbids it, but prompts are
aspirational; this is the enforcement. Given the unified diff of a change, flag:

  - net removal of test functions in test files, and
  - newly-added skip/ignore markers (pytest skip/xfail, JS xit/.skip, JUnit
    @Disabled/@Ignore, Go t.Skip, …).

Pure + language-agnostic (operates on diff text, no repo access) so it's trivially
unit-tested and reusable. The caller decides the consequence (fail the gate →
retry with feedback, or surface for review).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A path is "test code" if it looks like a test/spec file (covers py/js/ts/go/
# java/cs conventions). Kept broad on purpose — a false positive just means we
# scrutinize a non-test file's "def test_"-shaped lines, which is harmless.
_TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__)(/|_)|[._](test|spec)\.|test_.*\.py|_test\.(go|py)", re.IGNORECASE)

# A line that declares a test (added/removed). Multi-language.
_TEST_DECL = re.compile(
    r"\bdef\s+test\w*\s*\(|"                 # python: def test_x(
    r"\b(it|test)\s*\(|"                     # js/ts: it("...") / test("...")
    r"\bfunc\s+Test\w*\s*\(|"                # go: func TestX(
    r"@Test\b|\[Test\]|\[Fact\]|\[Theory\]", # java/junit, c#/xunit
    re.IGNORECASE,
)

# A line that disables a test (only counted when ADDED).
_SKIP_MARKER = re.compile(
    r"@pytest\.mark\.(skip|xfail)|@unittest\.skip|pytest\.skip\(|"
    r"\b(xit|xdescribe)\s*\(|\.(skip|only)\s*\(|"          # js/ts: xit(, it.skip(, it.only(
    r"@Disabled\b|@Ignore\b|"                              # junit5 / junit4
    r"\bt\.Skip\(|\bt\.SkipNow\(",                         # go
    re.IGNORECASE,
)


@dataclass
class IntegrityReport:
    removed_tests: int = 0
    added_skips: int = 0
    files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.removed_tests <= 0 and self.added_skips <= 0

    def summary(self) -> str:
        bits = []
        if self.removed_tests > 0:
            bits.append(f"{self.removed_tests} test function(s) removed")
        if self.added_skips > 0:
            bits.append(f"{self.added_skips} skip/ignore marker(s) added")
        where = f" in {', '.join(sorted(set(self.files)))}" if self.files else ""
        return ("test-integrity: " + "; ".join(bits) + where) if bits else "test-integrity: clean"


def scan_diff(diff_text: str) -> IntegrityReport:
    """Scan a unified diff for test-weakening. Counts removed test declarations
    (net of added ones) and newly-added skip markers, only within test files."""
    report = IntegrityReport()
    cur_is_test = False
    cur_file = ""
    added_decls = 0
    removed_decls = 0
    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git "):
            # the git header carries the path on BOTH sides even for a deletion
            # (where the +++ line is /dev/null) — the reliable filename source.
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            cur_file = m.group(2).strip() if m else ""
            cur_is_test = bool(_TEST_PATH.search(cur_file))
            continue
        if line.startswith("+++ "):
            p = line[4:].strip()
            if p and p != "/dev/null":  # don't let a deletion's /dev/null clobber
                cur_file = p[2:] if p.startswith(("a/", "b/")) else p
                cur_is_test = bool(_TEST_PATH.search(cur_file))
            continue
        if line.startswith(("--- ", "index ", "@@")):
            continue
        if not cur_is_test:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            body = line[1:]
            if _TEST_DECL.search(body):
                added_decls += 1
            if _SKIP_MARKER.search(body):
                report.added_skips += 1
                report.files.append(cur_file)
        elif line.startswith("-") and not line.startswith("---"):
            if _TEST_DECL.search(line[1:]):
                removed_decls += 1
                report.files.append(cur_file)
    # net removed test declarations (renames/moves add+remove → net ~0)
    report.removed_tests = max(0, removed_decls - added_decls)
    return report
