"""Mechanical E2E coverage gate — UI changes must ship Playwright specs.

The verify gate proves tests pass; test-integrity proves they weren't gutted —
but neither one notices when an agent *adds* a UI feature with no E2E coverage at
all. That's how a green run can ship a broken dashboard: the test suite simply
never exercised the new page. This module closes that hole.

Pure (operates on diff text — no repo access) and mechanical (no LLM call). Same
posture as ``loom/test_integrity.py``: language-agnostic-on-the-margins regex,
opinionated on the spec convention (``*.spec.ts/tsx/js/jsx`` and ``*.e2e.*``)
because that's what the ``30-e2e-playwright`` skill tells agents to write.

The substance check guards against the obvious dodge — an empty spec file added
just to satisfy the gate. A new spec must contain at least one test declaration
(``test(``, ``it(``, ``describe(``) in the additions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Clearly-UI extensions. Narrower than the visual gate's heuristic on purpose:
# a bare ``.ts`` file (API route, util) shouldn't require a Playwright spec.
# Component / view / style files do.
_UI_RE = re.compile(r"\.(tsx|jsx|vue|svelte|html|css|scss)$", re.IGNORECASE)

# Spec file conventions Playwright + Jest + Vitest converge on. Match either
# ``.spec.`` or ``.e2e.`` markers. Intentionally narrower than every test file
# (``.test.``) — the gate exists to push toward proper E2E coverage, not unit
# tests of a button component.
_SPEC_RE = re.compile(r"\.(spec|e2e)\.(ts|tsx|js|jsx)$", re.IGNORECASE)

# A spec line that actually declares something — Playwright/Jest/Vitest shapes.
# Used only to detect "this new spec file is hollow".
_TEST_DECL = re.compile(
    r"\b(test|it|describe)\s*[\.\(]|"   # test(, it(, describe(, test.skip( etc.
    r"\btest\.describe\s*\(",
    re.IGNORECASE,
)


@dataclass
class _FileState:
    path: str = ""
    is_ui: bool = False
    is_spec: bool = False
    is_new: bool = False
    has_additions: bool = False
    has_test_decl: bool = False


@dataclass
class CoverageReport:
    #: UI files that received additions/modifications in the diff
    ui_files: list[str] = field(default_factory=list)
    #: spec files that received additions/modifications in the diff
    spec_files: list[str] = field(default_factory=list)
    #: newly-created spec files with no detected test declaration (the dodge)
    hollow_specs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """The gate passes iff the diff doesn't add/modify UI without also adding
        a non-hollow spec. Pure-backend / spec-only / unchanged diffs all pass."""
        if not self.ui_files:
            return True  # nothing to gate
        if not self.spec_files:
            return False  # UI shipped without coverage
        if self.hollow_specs:
            return False  # a new spec is empty — substance dodge
        return True

    def summary(self) -> str:
        if self.ok:
            return "e2e-coverage: clean"
        bits: list[str] = []
        if self.ui_files and not self.spec_files:
            bits.append(
                f"{len(self.ui_files)} UI file(s) changed with no Playwright "
                f"spec added/modified ({', '.join(sorted(self.ui_files)[:5])})"
            )
        if self.hollow_specs:
            bits.append(
                f"new spec(s) without any test/it/describe call: "
                f"{', '.join(sorted(set(self.hollow_specs)))}"
            )
        return "e2e-coverage: " + "; ".join(bits)


def scan_diff(diff_text: str) -> CoverageReport:
    """Scan a unified diff for E2E coverage. Returns a report; the caller
    decides the consequence (block / warn / ignore)."""
    files: list[_FileState] = []
    cur = _FileState()
    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git "):
            if cur.path:
                files.append(cur)
            cur = _FileState()
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            cur.path = m.group(2).strip() if m else ""
            cur.is_ui = bool(_UI_RE.search(cur.path))
            cur.is_spec = bool(_SPEC_RE.search(cur.path))
            continue
        if line.startswith("--- "):
            # /dev/null on the `---` side means this file is net-new
            cur.is_new = "/dev/null" in line
            continue
        if line.startswith(("+++ ", "@@", "index ")):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            cur.has_additions = True
            if cur.is_spec and _TEST_DECL.search(line[1:]):
                cur.has_test_decl = True
    if cur.path:
        files.append(cur)

    return CoverageReport(
        ui_files=[f.path for f in files if f.is_ui and f.has_additions],
        spec_files=[f.path for f in files if f.is_spec and f.has_additions],
        hollow_specs=[
            f.path for f in files
            if f.is_spec and f.is_new and not f.has_test_decl
        ],
    )


def format_feedback(report: CoverageReport) -> str:
    """Render a failing report as actionable feedback for the retry loop."""
    lines = ["E2E coverage gate blocked this change:"]
    if report.ui_files and not report.spec_files:
        lines.append(
            "You modified UI files but did NOT add or update any Playwright "
            "spec (`*.spec.ts` / `*.e2e.ts`). Cover every page your change "
            "touches with a real spec that navigates, interacts (create / "
            "submit / etc.), and asserts the user-visible result."
        )
        lines.append("UI files in this change: " + ", ".join(sorted(report.ui_files)[:8]))
    if report.hollow_specs:
        lines.append(
            "New spec file(s) contain no `test(` / `it(` / `describe(` call — "
            "an empty spec does not satisfy coverage. Write the test that "
            "exercises the change."
        )
        lines.append("Hollow spec file(s): " + ", ".join(sorted(set(report.hollow_specs))))
    lines.append(
        "Do not weaken the coverage rule by deleting other specs or moving "
        "the change to a non-UI file. Add the missing spec, then re-verify."
    )
    return "\n".join(lines)
