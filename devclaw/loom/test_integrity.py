"""Detect the cheapest way to fake a green gate: weakening the tests.

The verify gate only proves "the test command exited 0" — which an agent under
pressure can satisfy by **deleting a failing test, skipping it, or gutting its
assertions** rather than fixing the code. The prompt forbids it, but prompts are
aspirational; this is the enforcement. Given the unified diff of a change, flag:

  - net removal of test functions in test files, and
  - newly-added skip/ignore markers (pytest skip/xfail, JS xit/.skip, JUnit
    @Disabled/@Ignore, Go t.Skip, …).

``scan_diff`` is pure + language-agnostic (operates on diff text, no repo access)
so it's trivially unit-tested. The caller decides the consequence (fail the gate
→ retry with feedback, or surface for review).

**Relocation credit (2026-07-17).** A pure-deletion diff that removes a test file
whose methods were already ported into another file *in a prior merged PR* shows
N removals and zero additions in THIS diff — so the count alone reads it as a
weakening, even though every test still lives in the suite. That mislabelling
cost closeloop-bench ~40h of thrash (four attempts, three strategies, all
rejected). The fix does NOT weaken the gate: ``scan_diff`` also extracts the
*names* of the removed tests, and the repo-aware :func:`present_test_names` lets
the caller CREDIT a removed test only when a same-named test declaration is
proven to still exist elsewhere in the post-change tree. No proof (name not
extractable, or not found) ⇒ it stays flagged — fail closed. Grounded relaxation,
never a blind allow.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# A path is "test code" if it looks like a test/spec file (covers py/js/ts/go/
# java/cs conventions). Kept broad on purpose — a false positive just means we
# scrutinize a non-test file's "def test_"-shaped lines, which is harmless.
_TEST_PATH = re.compile(
    r"(^|/)(tests?|spec|__tests__)(/|_)|"   # tests/ spec/ __tests__/ dirs, test_/spec_
    r"[._](test|spec)\.|"                   # foo.test. / foo_spec.
    r"test_.*\.py|_test\.(go|py)|"          # python/go conventions
    r"\.tests?(/|$)|"                       # C#/.NET *.Tests/ project dir (Domain.Tests/…)
    r"tests?\.(cs|java|kt|scala)$",         # PascalCase *Tests.cs / *Test.java files
    re.IGNORECASE,
)

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

# --- test-name extraction (for relocation credit) ----------------------------
# Inline-named declarations: the name is on the SAME line.
_PY_NAME = re.compile(r"\bdef\s+(test\w*)\s*\(")
_GO_NAME = re.compile(r"\bfunc\s+(Test\w*)\s*\(")
_JS_NAME = re.compile(r"\b(?:it|test)\s*\(\s*['\"`]([^'\"`]+)")
# C#/Java: an attribute line marks the NEXT method signature as a test — the name
# is on a following line, so it's paired statefully.
_ATTR_DECL = re.compile(r"@Test\b|\[Test\]|\[Fact\]|\[Theory\]", re.IGNORECASE)
_METHOD_SIG = re.compile(r"\b(?:public|internal|protected|private)\b[^;={]*?\b(\w+)\s*\(")


def _iter_test_names(lines):
    """Yield test identifiers declared in a sequence of code lines (no diff
    prefixes). Handles the inline languages directly and the C#/Java
    attribute→signature pairing statefully. Shared by the diff scan (removed
    lines) and the tree scan (whole files) so both speak one definition of
    "a test's name"."""
    pending_attr = False
    for line in lines:
        for rx in (_PY_NAME, _GO_NAME, _JS_NAME):
            m = rx.search(line)
            if m:
                yield m.group(1)
        if _ATTR_DECL.search(line):
            m = _METHOD_SIG.search(line)
            if m:
                yield m.group(1)      # attribute + signature on ONE line
            else:
                pending_attr = True   # signature is on a following line
            continue
        if pending_attr:
            m = _METHOD_SIG.search(line)
            if m:
                yield m.group(1)
                pending_attr = False
            elif line.strip() and not line.lstrip().startswith(("[", "//", "/*", "*")):
                # a real code line that isn't a signature closes the window
                pending_attr = False


@dataclass
class IntegrityReport:
    removed_tests: int = 0
    added_skips: int = 0
    files: list[str] = field(default_factory=list)
    #: names of the removed test declarations (best-effort per language) — the
    #: caller checks these against the post-change tree to credit relocations.
    removed_names: list[str] = field(default_factory=list)

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
    (net of added ones) and newly-added skip markers, only within test files.
    Also collects the names of removed tests (for the caller's relocation
    credit). Pure — no repo access."""
    report = IntegrityReport()
    cur_is_test = False
    cur_file = ""
    added_decls = 0
    removed_decls = 0
    removed_bodies: list[str] = []  # removed lines in test files, for name extraction
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
            body = line[1:]
            if _TEST_DECL.search(body):
                removed_decls += 1
                report.files.append(cur_file)
            removed_bodies.append(body)
    # net removed test declarations (renames/moves add+remove → net ~0)
    report.removed_tests = max(0, removed_decls - added_decls)
    report.removed_names = list(_iter_test_names(removed_bodies))
    return report


#: dirs never worth walking for test files — vendored deps and build output.
_SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", "bin", "obj", ".angular",
    ".venv", "venv", "__pycache__", "target", ".next", "coverage",
}


def present_test_names(workspace_dir: str, *, max_depth: int = 10) -> set[str]:
    """Every test identifier currently declared anywhere in ``workspace_dir``'s
    test files — the post-change tree. Used to CREDIT a removed test whose name
    still lives elsewhere (a move/dedup, not a weakening). Bounded walk, skips
    vendor/build dirs; best-effort — an unreadable file is skipped. Repo-aware
    (fs read) so it lives here beside the extractor but is never called by the
    pure ``scan_diff``; the caller invokes it only when a scan actually flags
    a removal, never on an idle path."""
    present: set[str] = set()
    for root, dirs, files in os.walk(workspace_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        if root[len(workspace_dir):].count(os.sep) > max_depth:
            dirs[:] = []
            continue
        for fn in files:
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, workspace_dir)
            if not _TEST_PATH.search(rel):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            present.update(_iter_test_names(text.splitlines()))
    return present
