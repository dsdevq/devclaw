"""Chef-side goal admission — "verified on all sides" before the chef accepts.

Today `create_goal` is best-effort: it warns on dubious shape (bare verify_cmd)
but accepts every goal regardless. That makes the waiter (or any caller) the
only line of defense against malformed orders. The chef has no standards.

This module is the standards. `verify_goal` runs structural checks against a
goal's parameters and returns a list of `AdmissionCondition`s with severity
``reject`` or ``warn``. `create_goal` consumes the result: any ``reject``
becomes a structured rejection (caller must fix and re-file); ``warn`` items
flow through to the result dict as before.

Why machine-readable codes (not just prose): the waiter (or any upstream chain
link) needs to ROUTE on the rejection — e.g. an `undecomposable_done_when`
rejection should loop back to the grill, while `missing_domain_research` should
trigger the domain-research module once it exists. Free-text doesn't route.

v1 scope (deliberately tight):
- Hard mechanical checks only. No LLM cognition.
- The set of codes is small. Add new ones when a real gap surfaces in the
  chain test or in production, not speculatively.
- `missing_domain_research_*` is deferred until the domain-research module
  exists (no point rejecting on something that can't be produced).

See ~/memory/projects/devclaw/chain-map-2026-06-30.md row 18 for the design
context and the gaps this closes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Severity = Literal["reject", "warn"]


@dataclass(frozen=True)
class AdmissionCondition:
    """One thing wrong (or worth flagging) about a goal at admission time.

    ``code`` is the machine-readable identifier the waiter routes on.
    ``message`` is the human-readable explanation for the owner.
    ``field`` names the goal field at fault when applicable (e.g.
    ``"done_when"``); ``None`` when the condition is cross-cutting.
    """

    code: str
    message: str
    severity: Severity = "reject"
    field: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"code": self.code, "severity": self.severity, "message": self.message}
        if self.field:
            d["field"] = self.field
        return d


@dataclass(frozen=True)
class AdmissionResult:
    """The verdict from :func:`verify_goal`. ``admitted`` is False iff any
    condition is severity ``reject``. ``conditions`` is the full list (rejects
    + warns) in declaration order — useful for the waiter to render."""

    admitted: bool
    conditions: list[AdmissionCondition] = field(default_factory=list)

    @property
    def rejections(self) -> list[AdmissionCondition]:
        return [c for c in self.conditions if c.severity == "reject"]

    @property
    def warnings(self) -> list[AdmissionCondition]:
        return [c for c in self.conditions if c.severity == "warn"]

    def to_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "conditions": [c.to_dict() for c in self.conditions],
        }


class GoalAdmissionRejected(Exception):
    """Raised by ``create_goal`` when admission has at least one reject-severity
    condition. Carries the full :class:`AdmissionResult` on ``.result`` so the
    MCP tool boundary can surface structured rejections to the caller."""

    def __init__(self, result: AdmissionResult) -> None:
        codes = ", ".join(c.code for c in result.rejections)
        super().__init__(f"goal admission rejected: {codes}")
        self.result = result


#: minimum char count for a ``done_when`` to be considered substantial. Vague
#: stubs like "ship it" / "make it better" / "build a CRM" are ~7-15 chars; a
#: real testable clause ("GET /health returns 200 with status:ok") is ~40+.
#: The bar is intentionally low — we're catching obvious laziness, not grading
#: completeness (which is the firming + decomposer's job).
_MIN_DONE_WHEN_CHARS = 20

import re as _re

#: a verify_cmd that's a single bare token (no path, no flags, no shell ops)
#: may not be on PATH inside the sandbox. Kept identical to the pre-admission
#: warning so we don't change behavior; just promoted into the admission seam.
_BARE_TOOL_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _check_objective(objective: str) -> Optional[AdmissionCondition]:
    if not objective or not objective.strip():
        return AdmissionCondition(
            code="missing_objective",
            message="objective is empty — the goal needs a one-line outcome statement.",
            severity="reject",
            field="objective",
        )
    return None


def _check_workspace_dir(workspace_dir: str) -> Optional[AdmissionCondition]:
    if not workspace_dir or not workspace_dir.strip():
        return AdmissionCondition(
            code="missing_workspace_dir",
            message="workspace_dir is empty — the chef needs a path to clone the repo into.",
            severity="reject",
            field="workspace_dir",
        )
    return None


def _check_done_when(done_when: str, spec: str) -> Optional[AdmissionCondition]:
    """Reject when there's nothing for the evaluator to grade against: empty
    ``done_when`` AND no ``spec`` (which carries acceptance criteria the
    evaluator can derive done_when clauses from). At least one must be present."""
    if (done_when and done_when.strip()) or (spec and spec.strip()):
        return None
    return AdmissionCondition(
        code="missing_done_when_and_no_spec",
        message=(
            "done_when is empty and no spec was provided — the evaluator has "
            "nothing to grade against. Provide a done_when (a verifiable "
            "completion statement) OR a spec (with acceptance criteria the "
            "chef can derive done_when from)."
        ),
        severity="reject",
        field="done_when",
    )


def _check_vague_done_when(done_when: str) -> Optional[AdmissionCondition]:
    """Reject obviously-vague done_when strings — short stubs like 'ship it' or
    'build a CRM' that can't decompose into atomic clauses. Length-only
    heuristic; intentionally simple to avoid false-positives on concise but
    valid statements like 'GET /health returns 200 with status:ok'."""
    if not done_when:
        return None
    stripped = done_when.strip()
    if len(stripped) >= _MIN_DONE_WHEN_CHARS:
        return None
    return AdmissionCondition(
        code="vague_done_when",
        message=(
            f"done_when {stripped!r} is too short ({len(stripped)} chars; need "
            f"at least {_MIN_DONE_WHEN_CHARS}) to express a verifiable "
            "completion test. Spell out specifically what 'done' means — what "
            "endpoint, what test, what observable behaviour confirms it."
        ),
        severity="reject",
        field="done_when",
    )


def _check_scope_anchor_for_from_scratch(
    *,
    repo_url: Optional[str],
    spec: str,
    backlog: list[str],
) -> Optional[AdmissionCondition]:
    """From-scratch goals (no ``repo_url``) need SOME anchor for the chef to
    plan against: a spec (preferred), or at least a starting backlog. Without
    either, the decomposer has only the objective + done_when and is likely
    to invent shape rather than reflect intent."""
    if repo_url:
        return None  # existing repo — the discovery brief will be the anchor
    if (spec and spec.strip()) or backlog:
        return None
    return AdmissionCondition(
        code="no_scope_anchor_for_from_scratch",
        message=(
            "this is a from-scratch goal (no repo_url) but no spec and no "
            "backlog were provided. The decomposer has nothing concrete to "
            "plan against and will invent shape. Run scope_grill to produce a "
            "spec, or pass a starting backlog of acceptance items."
        ),
        severity="reject",
        field="spec",
    )


def _check_skills_required(skills_required: list[str]) -> list[AdmissionCondition]:
    """Validate declared skill slugs against the host library.

    Three cases:
      - Caller declared no skills → no conditions.
      - Caller declared skills, library exists → reject if any slug is unknown.
      - Caller declared skills, library doesn't exist → warn (not reject) so
        dev environments without a populated library still work; the operator
        sees the warning and either populates the library or drops the
        declaration.
    """
    if not skills_required:
        return []
    from ..skill_library import list_available, library_path

    out: list[AdmissionCondition] = []
    available = list_available()
    lib = library_path()
    if not lib.is_dir():
        out.append(AdmissionCondition(
            code="skill_library_missing",
            message=(
                f"skills_required declared {skills_required!r} but the skill "
                f"library at {lib} does not exist. Skills will be silently "
                f"skipped at workspace prep. Populate the library or set "
                f"DEVCLAW_SKILL_LIBRARY to a directory of <slug>.md files."
            ),
            severity="warn",
            field="skills_required",
        ))
        return out
    unknown = [s for s in skills_required if s not in available]
    if unknown:
        out.append(AdmissionCondition(
            code="unknown_skill_required",
            message=(
                f"skills_required references unknown skills: {unknown!r}. "
                f"Available in the library: {available!r}. Drop the unknown "
                f"slugs or add the skill files to {lib}."
            ),
            severity="reject",
            field="skills_required",
        ))
    return out


def _check_standing_done_when(done_when: str) -> Optional[AdmissionCondition]:
    """Warn (not reject) when done_when disclaims boundedness. Standing goals
    are a legitimate shape (the closeloop missions are exactly this), but the
    owner should know what they filed: the done-gate will NEVER terminally
    close such a goal — an all-axes-pass verdict becomes ``needs_human`` and
    the close decision comes back to the owner. Filed as a warn so mission
    goals stay filable; reject would outlaw the pattern."""
    from .models import is_standing

    if not done_when or not is_standing(done_when):
        return None
    return AdmissionCondition(
        code="standing_done_when",
        message=(
            "done_when declares this a STANDING goal (unbounded — no terminal "
            "completion state). The done-gate will never close it 'achieved': "
            "when everything passes, it blocks with needs_human and hands the "
            "close decision to you. If you want terminal completion, file a "
            "bounded done_when instead."
        ),
        severity="warn",
        field="done_when",
    )


def _check_bare_verify_cmd(verify_cmd: Optional[str]) -> Optional[AdmissionCondition]:
    """Pre-admission this was a warning (and stays a warning here). A bare
    tool name may not be on PATH inside the sandbox — flag, don't reject."""
    if not verify_cmd:
        return None
    stripped = verify_cmd.strip()
    if not stripped or not _BARE_TOOL_RE.match(stripped):
        return None
    return AdmissionCondition(
        code="bare_verify_cmd",
        message=(
            f"verify_cmd {stripped!r} looks like a bare tool name — it may "
            f"fail if {stripped!r} is not on PATH inside the sandbox. "
            f"Consider 'python -m {stripped}' or a full path instead."
        ),
        severity="warn",
        field="verify_cmd",
    )


def verify_goal(
    *,
    objective: str,
    workspace_dir: str,
    done_when: str = "",
    backlog: Optional[list[str]] = None,
    repo_url: Optional[str] = None,
    verify_cmd: Optional[str] = None,
    spec: str = "",
    skills_required: Optional[list[str]] = None,
) -> AdmissionResult:
    """Run all admission checks against a candidate goal's parameters. Pure
    function — does NOT touch the store, does NOT raise. Returns the full
    :class:`AdmissionResult` so callers can route on conditions.

    Used by:
      - :meth:`GoalService.create_goal` — checks before filing; raises
        :class:`GoalAdmissionRejected` when ``admitted`` is False.
      - The ``verify_goal`` MCP tool — pre-flight check the waiter calls
        before ``create_goal`` so the customer sees fixable conditions before
        thinking the order was filed.

    Ordering rule: checks run in dependency order — missing-field checks fire
    first (so we don't run a vagueness check on an empty done_when), then
    shape checks, then severity-``warn`` checks last. Output order matches.
    """
    backlog = list(backlog or [])
    conditions: list[AdmissionCondition] = []

    # 1. presence checks — bail-shape early so later checks have something to
    #    inspect (though we DO continue running to surface all conditions in
    #    one pass, not just the first one).
    for check in (
        _check_objective(objective),
        _check_workspace_dir(workspace_dir),
        _check_done_when(done_when, spec),
    ):
        if check is not None:
            conditions.append(check)

    # 2. shape checks — only fire when the relevant field was provided.
    if done_when and done_when.strip():
        v = _check_vague_done_when(done_when)
        if v is not None:
            conditions.append(v)

    # 3. cross-cutting shape — from-scratch needs SOME anchor.
    anchor = _check_scope_anchor_for_from_scratch(
        repo_url=repo_url, spec=spec, backlog=backlog,
    )
    if anchor is not None:
        conditions.append(anchor)

    # 4. skill validation — mixed severity (reject for unknown slugs, warn
    # when the library is missing entirely).
    conditions.extend(_check_skills_required(list(skills_required or [])))

    # 5. warnings (do not block admission).
    for w in (
        _check_standing_done_when(done_when),
        _check_bare_verify_cmd(verify_cmd),
    ):
        if w is not None:
            conditions.append(w)

    admitted = not any(c.severity == "reject" for c in conditions)
    return AdmissionResult(admitted=admitted, conditions=conditions)
