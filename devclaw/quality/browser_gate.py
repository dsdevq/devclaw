"""Browser-E2E verification gate — "was the UI change actually exercised in a
real browser, or did it just pass unit tests + a static diff review?".

The gap this closes (2026-07-17, finance-sentry-ui-library): a UI change ships
green when ``verify_cmd`` is ``ng build && vitest`` — jsdom unit tests + a build,
never a browser. The adversarial review gate reads a *static diff*; the done-gate
reviewer reads *code*. Nothing boots the app in Chromium. A component like
``cmn-select`` passed every gate while throwing ``NG05105`` the instant its
dropdown opened, because no gate rendered the integrated app. The sandbox already
ships Chromium + ``@playwright/mcp`` (``.sandcastle/Dockerfile``,
``openhands-runner/sandbox-mcp.json``); what was missing is a deterministic
host-side assertion that a browser suite *actually ran and passed*.

This module is that assertion — a pure verdict function consulted at settle time,
the same shape as :mod:`devclaw.goal.remote_checks` (fail-open on *capability*
uncertainty, fail-closed on *evidence* of a broken or un-run browser suite).

Proof-of-execution contract (the crux, decided 2026-07-17): the verdict keys off
a machine-readable **Playwright JSON reporter summary** (``browser_report`` —
``{expected, unexpected, flaky, skipped}`` counts the runner parses from the
``--reporter=json`` artifact and attaches to the verify result). It does NOT
string-match ``verify_cmd`` for the word "playwright": that grep-shaped check
proves *intent*, not *execution*, and is exactly the ``evaluator.py`` existence-vs-
execution scar (a gate that asserted spec files EXISTED, not that they RAN). An
exit code of 0 is necessary but not sufficient — the gate requires a positive
*executed* count.

Verdict semantics:

- ``not_triggered`` → the diff touched no frontend path, OR every frontend path
                      it touched is library surface (``*/src/lib/*`` — a
                      library-only slice wires nothing into a running app, so a
                      full-app E2E has nothing to visit; its proof is the
                      story+spec the library build/test gate already requires) —
                      the browser gate is N/A for this change; never blocks.
                      The library exemption removes only the EXPECTATION of a
                      run: if a browser suite actually executed, its evidence
                      (``ran_passed``/``ran_failed``) is still processed in full.
- ``ran_passed``    → a browser suite executed (executed count > 0) and nothing
                      failed → gate satisfied.
- ``ran_failed``    → a browser suite executed and ≥1 test failed → blocks
                      (the correction retries with the failures fed back).
- ``never_ran``     → a frontend change shipped but no browser suite executed:
                      no ``browser_report`` at all, or a report with 0 executed
                      (exit 0 but nothing ran — the scar). The ``playwright``
                      config exists, so a browser run was expected — its absence
                      is evidence, not infrastructure. Blocks (fail closed).
- ``absent``        → a frontend change shipped and the project has NO playwright
                      config at all — nothing to run. Capability-shaped, like
                      ``remote_checks``' ``no_workflows``: blocks only under
                      ``strict`` (under ``flexible``, the default, it logs loudly
                      and falls through so a not-yet-E2E'd project isn't wedged).

``DEVCLAW_GOAL_BROWSER_GATE_MODE`` selects the stance (wired in a later change):
``flexible`` (default — a project with no browser suite degrades to a loud log
instead of a wedge) or ``strict`` (any frontend change with no passing browser
run blocks). ``ran_failed``/``never_ran`` block in BOTH modes — those are
evidence of a problem, not capability uncertainty.

Pure module — no subprocess, no I/O. The runner parses the artifact into
``browser_report`` in-sandbox; the settle path detects ``config_present`` on the
host; this function only folds those inputs into a verdict, so it stays a
trivially-testable unit (the ``remote_checks`` / ``merge`` seam shape).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

#: Frontend path globs (fnmatch, tested against each changed path in the diff).
#: A change touching any of these makes the browser gate REQUIRED. Mirrors the
#: pathspecs the worker-side ``post-run.sh`` check already uses. ``*`` spans
#: ``/`` under fnmatch, so ``*.component.ts`` matches ``frontend/src/app/x.component.ts``.
DEFAULT_FRONTEND_GLOBS: tuple[str, ...] = (
    "*.component.ts",
    "*.component.html",
    "*/src/app/*",
    "*angular.json",
    "*.component.scss",
    "*.component.css",
)

#: Library-source path globs. A frontend path under one of these is LIBRARY
#: surface, not app surface: a library-only slice wires nothing into a running
#: app route, so a full-app browser E2E has nothing to visit (the cmn-tab-group
#: wedge, 2026-07-18 — a library-only diff verdicted ``never_ran`` in both
#: modes and no retry could ever fix it). ``*/src/lib/*`` is the Angular
#: workspace library convention (``projects/<name>/src/lib/…``) and also covers
#: Nx (``libs/<name>/src/lib/…``). A library slice's browser-equivalent proof
#: is its story+spec, which the library build/test gate already requires. A
#: diff whose frontend paths are ALL library surface exempts the gate; ANY
#: app-surface path (including ``angular.json``) keeps it required.
DEFAULT_LIBRARY_GLOBS: tuple[str, ...] = ("*/src/lib/*",)

#: Playwright config filenames that mark a project as having a browser suite.
PLAYWRIGHT_CONFIG_NAMES: tuple[str, ...] = (
    "playwright.config.ts",
    "playwright.config.js",
    "playwright.config.mjs",
    "playwright.config.cjs",
)

_DIFF_GIT_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)\s*$", re.MULTILINE)
_DIFF_PLUS_RE = re.compile(r"^\+\+\+ b/(?P<p>.+?)\s*$", re.MULTILINE)


def _fnmatch(path: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    return fnmatch(path, pattern)


def changed_paths(diff: str) -> list[str]:
    """The set of file paths a unified ``git diff`` touched. Reads both the
    ``diff --git a/X b/Y`` headers (present even for pure deletes/renames) and
    the ``+++ b/X`` hunk markers, so a path is caught regardless of change kind.
    ``/dev/null`` (delete target) is dropped."""
    paths: set[str] = set()
    for m in _DIFF_GIT_RE.finditer(diff or ""):
        paths.add(m.group("a"))
        paths.add(m.group("b"))
    for m in _DIFF_PLUS_RE.finditer(diff or ""):
        paths.add(m.group("p"))
    paths.discard("/dev/null")
    return sorted(paths)


def diff_touches_frontend(
    diff: str, globs: tuple[str, ...] = DEFAULT_FRONTEND_GLOBS
) -> bool:
    """Did this change touch a web-UI path that warrants a browser run? Pure
    path matching over the diff — the trigger is mechanical and diff-driven, NOT
    a goal declaration, precisely so a UI change that *didn't* declare it needs a
    browser is still caught."""
    return any(_fnmatch(p, g) for p in changed_paths(diff) for g in globs)


def diff_is_library_only(
    diff: str,
    globs: tuple[str, ...] = DEFAULT_FRONTEND_GLOBS,
    library_globs: tuple[str, ...] = DEFAULT_LIBRARY_GLOBS,
) -> bool:
    """Is every frontend-matching path in this diff library surface? True only
    when the diff touches at least one frontend path AND each such path also
    matches a library glob — then there is no app surface for a browser run to
    exercise and the gate is N/A. One app-surface path (an ``src/app`` file,
    ``angular.json``, an app component outside ``src/lib``) makes this False and
    the gate stays REQUIRED. Non-frontend paths (docs, package.json, stories,
    specs) never influence the decision — the gate only ever keyed on frontend
    surface. Same mechanical, diff-driven shape as :func:`diff_touches_frontend`:
    a trigger-scoping rule, never a verdict-weakening one."""
    frontend = [
        p
        for p in changed_paths(diff)
        if any(_fnmatch(p, g) for g in globs)
    ]
    return bool(frontend) and all(
        any(_fnmatch(p, g) for g in library_globs) for p in frontend
    )


@dataclass(frozen=True)
class BrowserGateResult:
    """The browser-gate verdict for one settled task."""

    state: str  # not_triggered | ran_passed | ran_failed | never_ran | absent
    detail: str = ""

    def blocks_delivery(self, mode: str = "flexible") -> bool:
        """Does this verdict fail the change CLOSED under ``mode``? Evidence of a
        broken or un-run browser suite (``ran_failed`` / ``never_ran``) blocks in
        both modes; the capability-shaped ``absent`` blocks only under
        ``strict`` — mirrors :meth:`remote_checks.RemoteChecksResult.blocks_done`."""
        if self.state in ("ran_failed", "never_ran"):
            return True
        if self.state == "absent":
            return mode == "strict"
        return False


def _executed(report: dict) -> int:
    """Tests a Playwright JSON run actually executed = expected(pass) + unexpected
    (fail) + flaky. ``skipped`` does NOT count — a suite that skipped everything
    ran nothing, the scar's signature."""
    return sum(
        int(report.get(k, 0) or 0) for k in ("expected", "unexpected", "flaky")
    )


def browser_run_verdict(
    verify_result: Optional[dict],
    diff: str,
    *,
    config_present: bool,
    globs: tuple[str, ...] = DEFAULT_FRONTEND_GLOBS,
    library_globs: tuple[str, ...] = DEFAULT_LIBRARY_GLOBS,
) -> BrowserGateResult:
    """Fold (the verify result's ``browser_report``, the diff, whether a
    playwright config exists) into a browser-gate verdict. Pure.

    ``browser_report`` is the compact Playwright JSON summary the runner attaches
    to the verify dict (``{expected, unexpected, flaky, skipped}``); its absence
    means no browser suite reported a run. ``config_present`` is whether the
    workspace has a ``playwright.config.*`` at all — the capability signal that
    separates ``never_ran`` (config exists, run expected, none happened) from
    ``absent`` (no suite to run)."""
    if not diff_touches_frontend(diff, globs):
        return BrowserGateResult("not_triggered", "no frontend path in the diff")

    # The library-only exemption removes the EXPECTATION of a browser run — it
    # is consulted only on the no-run paths below. Evidence from a run that
    # actually happened (a failing or passing report) is still processed in
    # full: trigger-scoping, never verdict-weakening.
    _library_only = BrowserGateResult(
        "not_triggered",
        "library-only frontend diff (every UI path under a library glob, no "
        "app surface changed) — nothing for a full-app browser run to visit; "
        "a library slice's browser-equivalent proof is its story+spec, which "
        "the library build/test gate already requires",
    )

    report = (verify_result or {}).get("browser_report")
    if not isinstance(report, dict):
        if diff_is_library_only(diff, globs, library_globs):
            return _library_only
        if not config_present:
            return BrowserGateResult(
                "absent",
                "frontend changed but the project has no playwright.config.* — "
                "no browser suite to run",
            )
        return BrowserGateResult(
            "never_ran",
            "frontend changed and a playwright config exists, but no browser run "
            "was reported — the suite did not execute (exit 0 is not proof of a run)",
        )

    executed = _executed(report)
    unexpected = int(report.get("unexpected", 0) or 0)
    skipped = int(report.get("skipped", 0) or 0)
    summary = (
        f"expected={report.get('expected', 0)} unexpected={unexpected} "
        f"flaky={report.get('flaky', 0)} skipped={skipped}"
    )
    if executed == 0:
        if diff_is_library_only(diff, globs, library_globs):
            return _library_only
        return BrowserGateResult(
            "never_ran",
            f"a browser report exists but 0 tests executed ({summary}) — "
            f"the suite ran nothing (the existence-vs-execution scar)",
        )
    if unexpected > 0:
        return BrowserGateResult("ran_failed", f"{unexpected} browser test(s) failed ({summary})")
    return BrowserGateResult("ran_passed", f"{executed} browser test(s) passed ({summary})")


def config_present_in(workspace_files: list[str]) -> bool:
    """Whether any ``playwright.config.*`` sits at the root of a file listing.
    Split out so the settle path (host-side ``os.listdir``) and tests share one
    definition of "the project has a browser suite"."""
    base = {f.rsplit("/", 1)[-1] for f in workspace_files}
    return any(name in base for name in PLAYWRIGHT_CONFIG_NAMES)
