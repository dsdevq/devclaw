"""Chain test — one CRM fixture walked end-to-end through devclaw's
alignment-then-execution chain.

This is NOT a cognition-quality test. It grades whether devclaw's modules
orchestrate the work the way a competent engineering team would: did the
right links fire, in the right order, with each link's output a reasonable
input to the next?

The chain is the artifact under test. The test walks ALL links — even where a
module is missing today — and collects gaps in a list it prints at the end.
The assertion at the bottom fails the test when gaps exist, so the test acts
as a running TODO list: as gaps fill, fail-count drops.

Opt-in via DEVCLAW_RUN_CHAIN_EVALS=1 (burns real claude quota: ~grill turns +
1 decomposer call).

See tests/chain/README.md and ~/memory/projects/devclaw/chain-map-2026-06-30.md.
"""

from __future__ import annotations

import asyncio
import os
import textwrap
from typing import Any

import pytest

from devclaw.elicitation import next_step as grill_next_step
from devclaw.goal.decomposer import decompose, default_caller as decomposer_caller
from devclaw.goal.models import Goal

RUN_FLAG = "DEVCLAW_RUN_CHAIN_EVALS"
MAX_GRILL_TURNS = 12  # bound runaway grills; lower than the chef's 20 to keep tests fast

# The fixture: a vague idea + tech-stack hints. Deliberately vague — the chain
# only earns its keep if it can take an under-specified ask and produce
# something coherent at every link.
VAGUE_IDEA = textwrap.dedent("""\
    I want to build a CRM. React frontend, .NET backend, deployed on a single
    self-hosted VPS. MVP scope — keep it small. I'll use it for my own
    consulting practice (5-10 contacts at most for now).
""").strip()

# Synthetic user answers — list of (keyword-phrase, reply) tuples. Longer/more
# specific phrases come first so the matcher prefers them when multiple could
# fire. Single-word keys are deliberately avoided: previous version had "email"
# as a key, which false-matched a pipeline question containing "email" as an
# example of a contact field, sending a nonsense answer the grill choked on.
USER_REPLY_RULES: list[tuple[str, str]] = [
    ("pipeline or deal", "No pipeline or deal tracking in MVP — a single status field on the contact is the most I'd want."),
    ("deal tracking", "No deal tracking in MVP."),
    ("email integration", "No email integration in MVP."),
    ("email sync", "No email sync in MVP."),
    ("calendar integration", "No calendar integration in MVP."),
    ("csv import", "No CSV import in MVP — I'll add 5-10 contacts by hand."),
    ("bulk import", "No bulk import in MVP."),
    ("csv export", "No export in MVP."),
    ("data export", "No export in MVP."),
    ("file attachment", "No file attachments in MVP."),
    ("reminder", "No reminders or follow-up scheduling in MVP."),
    ("notification", "No notifications in MVP."),
    ("multi-user", "Just me — single user, single admin role."),
    ("team size", "Just me — single user."),
    ("multiple users", "Just me — single user."),
    ("role", "One role — me as the admin. No RBAC."),
    ("authentication", "Simple email + password login, no SSO."),
    ("auth method", "Email + password is fine."),
    ("deploy", "Self-hosted on one VPS behind Tailscale. No public exposure."),
    ("hosting", "Self-hosted on one VPS behind Tailscale."),
    ("database choice", "SQLite is fine for now; can swap to Postgres later."),
    ("database engine", "SQLite is fine; can swap to Postgres later."),
    ("which database", "SQLite is fine; can swap to Postgres later."),
    ("file storage", "Local disk, no S3."),
    ("which entities", "Contacts and per-contact interaction notes. That's it for MVP."),
    ("what entities", "Contacts and per-contact interaction notes. MVP only."),
    ("reporting", "No reporting in MVP."),
    ("dashboard", "No dashboard in MVP."),
    ("testing", "Unit tests for the backend, one Playwright smoke test for the frontend."),
    ("ci/cd", "Self-hosted GitHub Actions runner, runs build + tests on PR."),
    ("ci pipeline", "Self-hosted GitHub Actions runner."),
    ("scope", "Keep it minimal — anything optional is out."),
    ("mvp", "Strict MVP — anything optional is out. I can ship the next thing later."),
    ("search", "No search in MVP — scrollable list of 10 is fine."),
    ("filter", "No filtering in MVP."),
    ("mobile", "No mobile app — browser only."),
    ("https", "Plain HTTP for MVP; TLS later."),
    ("tls", "Plain HTTP for MVP; TLS later."),
    ("backup", "Manual pg_dump / file copy for now. No automation in MVP."),
]


def synth_user_reply(question: str, recommended: str) -> str:
    """Match the grill's question against the rule table; fall back to the
    grill's own recommendation. The rule list is ordered longest-phrase-first
    so specific matches win over generic ones — single-word keys are AVOIDED
    because they false-match contact-field examples ("email" matching a
    pipeline question that listed email as a contact field)."""
    q_lower = question.lower()
    for phrase, reply in USER_REPLY_RULES:
        if phrase in q_lower:
            return reply
    return recommended or "Use your recommendation — I trust your judgment on this."


def _hr(title: str) -> str:
    bar = "=" * 78
    return f"\n{bar}\n  {title}\n{bar}"


def _format_checklist(checklist) -> str:
    """Render a Checklist for stdout — milestone-grouped (when milestones are
    tagged), with each item's requirement + evidence target. Falls back to a
    single ``(no milestone)`` bucket when items are flat."""
    if not checklist or not checklist.items:
        return "(empty checklist)"

    by_ms: dict[str, list] = {}
    for item in checklist.items:
        ms = item.milestone or "(no milestone)"
        by_ms.setdefault(ms, []).append(item)

    lines = []
    for ms, items in by_ms.items():
        lines.append(f"\n  ## {ms}")
        for item in items:
            tier = f" [{item.model_tier}]" if getattr(item, "model_tier", None) else ""
            deps = f"  deps: {item.depends_on}" if item.depends_on else ""
            effort = f"  ~{item.effort_minutes}m" if item.effort_minutes else ""
            lines.append(f"    - {item.id}{tier}{effort}{deps}")
            lines.append(f"        requirement: {item.requirement}")
            lines.append(f"        evidence_target: {item.evidence_target}")
            if item.note:
                lines.append(f"        note: {item.note}")
    if checklist.open_questions:
        lines.append("\n  OPEN QUESTIONS for the owner:")
        for q in checklist.open_questions:
            lines.append(f"    - {q}")
    if checklist.notes:
        lines.append("\n  NOTES to the planner:")
        for n in checklist.notes:
            lines.append(f"    - {n}")
    return "\n".join(lines)


@pytest.mark.skipif(
    os.environ.get(RUN_FLAG) != "1",
    reason=f"chain evals are opt-in; set {RUN_FLAG}=1 to run (burns claude quota)",
)
def test_chain_crm_walk(capsys) -> None:
    """One CRM fixture, walked end-to-end. Prints every link's I/O; collects
    gaps; asserts no gaps at the end. Today the test SHOULD fail with several
    named gaps — that's the spec for what we build next."""
    gaps: list[str] = []

    asyncio.run(_walk_chain(gaps))

    print(_hr("GAPS SURFACED"))
    if not gaps:
        print("  (none — chain is complete)")
    else:
        for i, g in enumerate(gaps, 1):
            print(f"  {i}. {g}")
    print()

    assert not gaps, (
        f"chain has {len(gaps)} unfilled link(s) — see stdout for the gap list. "
        "Each gap is a TODO; the test passes when all are filled."
    )


async def _walk_chain(gaps: list[str]) -> None:
    # ---- Links 1-3: scope_grill → spec --------------------------------------
    print(_hr("LINK 1-3: scope_grill (vague idea → questions → spec)"))
    print("\nVAGUE IDEA:\n")
    print(textwrap.indent(VAGUE_IDEA, "  "))

    transcript: list[dict[str, str]] = []
    spec: str = ""
    for turn in range(1, MAX_GRILL_TURNS + 1):
        try:
            step = await grill_next_step(VAGUE_IDEA, transcript)
        except Exception as err:  # noqa: BLE001 — chain test wants to surface failures, not crash
            gaps.append(f"scope_grill raised on turn {turn}: {err!r}")
            break

        if step.get("action") == "done":
            spec = step.get("spec", "")
            print(f"\n--- grill finalized after {turn - 1} answered turn(s) ---")
            break

        question = step.get("question", "")
        recommended = step.get("recommended", "")
        answer = synth_user_reply(question, recommended)
        print(f"\nTurn {turn}:")
        print(f"  Q: {question}")
        print(f"  recommended: {recommended}")
        print(f"  user answer: {answer}")
        transcript.append({"question": question, "recommended": recommended, "answer": answer})
    else:
        gaps.append(
            f"scope_grill did not finalize within {MAX_GRILL_TURNS} turns — "
            "either the model is over-asking or the synthesized user answers "
            "aren't satisfying the grill's information needs."
        )

    if spec:
        print("\nFINAL SPEC:\n")
        print(textwrap.indent(spec.strip(), "  "))
    else:
        print("\n(no spec produced)")
        gaps.append("scope_grill produced no spec — downstream links cannot be exercised honestly")

    # ---- Link 4: user-agreement (waiter responsibility — decided 2026-06-30) -
    print(_hr("LINK 4: user-agreement gate"))
    print(
        "\n  Decided 2026-06-30: WAITER RESPONSIBILITY, not chef's. The spec →\n"
        "  user-approve → create_goal sequence happens in Telegram; the chef\n"
        "  receives a filed order via the MCP tool boundary. Admission control\n"
        "  (link 18) now provides the structural backstop — a goal whose spec\n"
        "  the user didn't approve will be missing the spec-derived shape\n"
        "  admission requires. See ~/memory/projects/devclaw/chain-map-2026-06-30.md."
    )

    # ---- Link 9: domain research --------------------------------------------
    print(_hr("LINK 9: domain research (look at real CRMs, distill MVP)"))
    from devclaw.goal.world_research import (
        default_caller as world_research_caller,
        should_fire as world_research_should_fire,
        world_brief,
    )

    # Build the same Goal we'll later hand to the decomposer so should_fire's
    # from-scratch rule sees the actual shape.
    _wr_goal = Goal(
        id="chain-crm-fixture",
        objective="Build a minimal CRM for one user (consultancy use). "
                  "React frontend, .NET backend, SQLite, self-hosted on one VPS. "
                  "MVP scope only.",
        cadence="1d", engine="openhands", workspace_dir="/tmp/chain-crm",
        repo_url=None, verify_cmd=None, open_pr=True,
        done_when="", backlog=[], stub_acceptable=[],
    )
    world_brief_text = ""
    if not world_research_should_fire(_wr_goal):
        print("\n  should_fire=False — goal has a repo_url, world-research skipped.")
    else:
        print("\n  should_fire=True — firing world-research (from-scratch CRM goal).")
        try:
            world_brief_text = await world_brief(
                _wr_goal, spec=spec, caller=world_research_caller(),
            )
        except Exception as err:  # noqa: BLE001
            gaps.append(f"world-research raised: {err!r}")
            print(f"\n  world-research FAILED: {err!r}")
        else:
            print("\nWORLD-RESEARCH BRIEF:\n")
            print(textwrap.indent(world_brief_text, "  "))

    # ---- Link 18: chef admission control ------------------------------------
    print(_hr("LINK 18: chef admission control (verified on all sides)"))
    from devclaw.goal.admission import verify_goal as _verify

    # The CRM fixture is from-scratch (no repo_url) with no done_when set on
    # the goal — but the grill produced a spec carrying acceptance criteria,
    # so admission's done_when-or-spec check should pass on the spec path.
    admission = _verify(
        objective="Build a minimal CRM for one user (consultancy use). "
                  "React frontend, .NET backend, SQLite, self-hosted on one VPS. "
                  "MVP scope only.",
        workspace_dir="/tmp/chain-crm",
        done_when="",  # intentionally — admission allows spec to carry it
        backlog=[],
        repo_url=None,
        spec=spec,
    )
    print(f"\n  admitted: {admission.admitted}")
    print(f"  conditions: {len(admission.conditions)}")
    for c in admission.conditions:
        print(f"    [{c.severity}] {c.code}: {c.message[:100]}{'...' if len(c.message) > 100 else ''}")
    if not admission.admitted:
        codes = ", ".join(c.code for c in admission.rejections)
        gaps.append(
            f"chef admission rejected the CRM fixture: {codes}. Either the "
            "fixture is malformed, or admission is over-rejecting. Decide which."
        )
    else:
        print("\n  Goal admitted — admission gap (formerly gap #3) is CLOSED.")
        # The waiter-altitude question — should admission ALSO require domain-
        # research evidence for from-scratch goals? Defer that decision until
        # the domain-research module exists.
        print(
            "  (Note: admission does NOT yet require domain-research evidence "
            "for from-scratch goals — deferred until that module exists.)"
        )

    # ---- Link 6: skills install ---------------------------------------------
    print(_hr("LINK 6: per-project skills install"))
    # Exercise the mechanism end-to-end against a tmp library — the chain
    # test doesn't run real workspace prep, but provisioning is pure I/O so
    # we can demonstrate the same code path the tick uses.
    import os as _os
    import tempfile as _tempfile
    from pathlib import Path as _Path
    from devclaw.skill_library import list_available as _list_skills, provision as _provision

    with _tempfile.TemporaryDirectory() as _td:
        _lib = _Path(_td) / "skill-library"
        _lib.mkdir()
        (_lib / "dotnet.md").write_text("# .NET engineering brief\n- xUnit\n- EF Core\n")
        (_lib / "react.md").write_text("# React engineering brief\n- Vite\n- Tailwind\n")
        _orig = _os.environ.get("DEVCLAW_SKILL_LIBRARY")
        _os.environ["DEVCLAW_SKILL_LIBRARY"] = str(_lib)
        try:
            print(f"\n  library: {_lib}")
            print(f"  available: {_list_skills()}")
            _ws = _Path(_td) / "ws"
            _ws.mkdir()
            result = _provision(_ws, ["dotnet", "react"])
            print(f"  goal declared: ['dotnet', 'react']")
            print(f"  provisioned:   {result.provisioned}")
            print(f"  missing:       {result.missing}")
            _files = sorted((_ws / ".agent" / "skills").glob("*.md"))
            print(f"  files landed:  {[p.name for p in _files]}")
            if result.missing or set(result.provisioned) != {"dotnet", "react"}:
                gaps.append(
                    "skill provisioning landed the wrong files on the CRM fixture — "
                    f"provisioned={result.provisioned}, missing={result.missing}"
                )
            else:
                print(
                    "\n  Per-project skill provisioning works end-to-end. The goal's\n"
                    "  `skills_required` field is round-tripped via goal.yaml and the\n"
                    "  runner picks them up from <workspace>/.agent/skills/ on every\n"
                    "  task dispatch (the prep step copies them in after git clean)."
                )
        finally:
            if _orig is None:
                _os.environ.pop("DEVCLAW_SKILL_LIBRARY", None)
            else:
                _os.environ["DEVCLAW_SKILL_LIBRARY"] = _orig

    # ---- Links 5/7/8: repo init, AGENTS.md, CI/CD ---------------------------
    print(_hr("LINKS 5/7/8: create_repo, onboard (AGENTS.md), CI/CD task"))
    print(
        "\n  create_repo + onboard EXIST as separate MCP tools; v2 of this chain\n"
        "  test will drive them in sequence (gh-authed environment + workspace\n"
        "  setup required). CI/CD is now a per-project engineering-judgment task\n"
        "  dispatched via implement_feature (plan.md §Production-ready C5), not a\n"
        "  hardcoded template scaffolder — the earlier setup_cicd MCP tool was\n"
        "  removed 2026-07-02 because its 5-stack template list silently\n"
        "  misgenerated for fullstack repos. Decided 2026-06-30: this is\n"
        "  intentional v2 scope, NOT an unfilled chain gap. Tracked in\n"
        "  chain-map row 18 + a separate task for the v2 harness."
    )

    # ---- Link 11: decomposition (the load-bearing eyeball checkpoint) -------
    print(_hr("LINK 11: decomposition (LOAD-BEARING EYEBALL CHECKPOINT)"))
    if not spec:
        print("\n  SKIPPED — no spec from earlier link, decomposer has nothing to work from.")
        return

    # Build a minimal Goal carrying what the chain produced so far. In a real
    # admit-then-decompose flow, this Goal would have a derived done_when from
    # firming + domain-research evidence; we don't have those modules yet, so
    # we pass the grilled spec as the objective and let the decomposer see it
    # as discovery brief context.
    goal = Goal(
        id="chain-crm-fixture",
        objective="Build a minimal CRM for one user (consultancy use). React frontend, .NET backend, SQLite, self-hosted on one VPS. MVP scope only.",
        cadence="1d",
        engine="openhands",
        workspace_dir="/tmp/chain-crm",  # not used — decomposer is pure cognition
        repo_url=None,
        verify_cmd=None,
        open_pr=True,
        done_when="",  # GAP — would come from firming after domain research
        backlog=[],
        stub_acceptable=[],
    )

    print("\nGoal handed to decomposer (synthesized from spec — would normally be firming output):")
    print(f"  objective: {goal.objective}")
    print(f"  done_when: (empty — see admission/firming gaps)")

    # Combine the spec + the world-research brief as the decomposer's
    # discovery context. For from-scratch goals the world brief is what
    # plays the role the repo analysis plays for existing-repo goals.
    combined_brief = spec
    if world_brief_text:
        combined_brief = (
            f"{spec}\n\n## World research — exemplars + MVP bar + defer list\n\n"
            f"{world_brief_text}"
        )

    try:
        checklist = await decompose(
            goal,
            claude_caller=decomposer_caller(),
            discovery_brief=combined_brief,
            repo_digest="",  # from-scratch — no repo yet
        )
    except Exception as err:  # noqa: BLE001
        gaps.append(f"decomposer raised: {err!r}")
        print(f"\n  decomposer FAILED: {err!r}")
        return

    print("\nDECOMPOSITION (eyeball this — is it a tree a senior IC would produce?):")
    print(_format_checklist(checklist))
    print(f"\n  total items: {len(checklist.items)}")
    items_with_deps = sum(1 for i in checklist.items if i.depends_on)
    print(f"  items with depends_on edges: {items_with_deps} / {len(checklist.items)}")
    milestones = {i.milestone for i in checklist.items if i.milestone}
    print(f"  distinct milestones tagged: {len(milestones)}")

    # Structural sanity checks — programmable, no model judgment.
    if not checklist.items:
        gaps.append("decomposer produced an empty checklist for the CRM goal")
    if len(checklist.items) < 4:
        gaps.append(
            f"decomposer produced only {len(checklist.items)} item(s) for a full CRM "
            "MVP — looks under-decomposed. Expected at least a handful covering "
            "repo setup, backend, frontend, and tests."
        )
    # The spec we fed in carries 4 milestones (M1–M4); we now expect the
    # decomposer to tag items with them. If the milestone tagging didn't
    # land, surface that — the schema accepts the field but the prompt may
    # have been ignored.
    if not milestones:
        gaps.append(
            "decomposer produced no milestone tags despite the spec listing "
            "milestones — prompt instruction may need strengthening."
        )
