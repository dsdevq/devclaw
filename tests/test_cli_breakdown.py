"""`devclaw cognition breakdown` — the no-docker FULL-spine dry-run.

Where `plan` / `decompose` each expose ONE cognition link, `breakdown` walks the
whole natural chain a durable goal runs over time: scope-grill (the waiter's
finalize turn) → firming round 1 → decomposer, rendered goal → milestones →
tasks. Fully stubbed — three fake callers return canned grill/firming/decomposer
output, so these exercise the real CLI wiring (build → call → parse → render)
with NO docker, task queue, state store, or real claude. Named behaviors:

- the default run chains all THREE callers (grill → firm → decompose) and makes
  exactly 3 cognition calls; the grilled spec AND the firming-derived done_when
  both reach the decomposer's prompt;
- `--no-firm` runs grill → decompose only (2 calls, firming never invoked);
- `--json` emits the spec, the per-stage chain, and the parsed checklist;
- like `plan`/`decompose`, it constructs no registry / GoalStore / StateStore
  (the queue + engine are never reachable) — inspection only.
"""
from __future__ import annotations

import json

import pytest

import devclaw.cli as cli
from devclaw.loom import trace as _trace


# ---- canned model responses ------------------------------------------------

_SPEC = (
    "# accounts — spec\n"
    "## Milestones\n"
    "1. **M1 — Accounts** — the accounts endpoints and their tests\n"
    "## Acceptance\n"
    "- GET /accounts returns the list\n"
)
_GRILL_DONE = json.dumps({"action": "done", "spec": _SPEC})

_FIRMED = (
    "status: firmed\n"
    "intent: Ship the accounts API\n"
    "success_criteria:\n"
    "  - id: c1\n"
    "    text: GET /accounts returns the list\n"
    "  - id: c2\n"
    "    text: green tests\n"
)

_CHECKLIST = """checklist:
  - id: item1
    requirement: Wire GetAccounts.Execute to the query handler
    evidence_target: src/Accounts/GetAccounts.cs calls IQueryHandler
    milestone: M1 — Accounts
    depends_on: []
  - id: item2
    requirement: Add an integration test for GET /accounts
    evidence_target: tests/AccountsTests.cs asserts a 200 with a real body
    milestone: M1 — Accounts
    depends_on: [item1]
open_questions:
  - which auth scheme should protect the endpoint?
notes: []
"""


class _FakeCaller:
    """A one-arg async caller that records ONE cognition event (so the CLI's
    latency/token surface + call-count are exercised) and captures its prompts."""

    def __init__(self, response: str, *, role: str) -> None:
        self.response = response
        self.role = role
        self.calls: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        _trace.record_cognition(
            role=self.role, model="opus", prompt=prompt, response=self.response,
            latency_ms=1000, tokens_in=900, tokens_out=300, cost_usd=0.01,
        )
        return self.response


@pytest.fixture
def chain(monkeypatch):
    grill = _FakeCaller(_GRILL_DONE, role="grill")
    firm = _FakeCaller(_FIRMED, role="goal_firming")
    dec = _FakeCaller(_CHECKLIST, role="goal_decomposer")
    monkeypatch.setattr(cli, "_default_grill_caller", lambda: grill)
    monkeypatch.setattr(cli, "_default_firming_caller", lambda: firm)
    monkeypatch.setattr(cli, "_default_decomposer_caller", lambda: dec)
    return grill, firm, dec


# ---------------------------------------------------------------------------


def test_breakdown_chains_grill_firm_decompose_into_milestone_checklist(chain, capsys):
    grill, firm, dec = chain
    rc = cli.main(["cognition", "breakdown", "Build the accounts API"])
    assert rc == 0

    # all three links fired, exactly once each → 3 cognition calls
    assert len(grill.calls) == 1
    assert len(firm.calls) == 1
    assert len(dec.calls) == 1

    # the grilled spec flowed into firming AND into the decomposer's brief
    assert "## Milestones" in firm.calls[0]
    assert "the accounts endpoints and their tests" in dec.calls[0]
    # the firming-derived done_when ("... AND green tests") reached the decomposer
    assert "green tests" in dec.calls[0]

    out = capsys.readouterr().out
    assert "cognition_calls=3" in out
    assert "containers=0" in out
    # milestone-grouped render of the atomic tasks
    assert "milestone: M1 — Accounts" in out
    assert "item1" in out and "item2" in out
    assert "Wire GetAccounts.Execute" in out


def test_breakdown_no_firm_skips_firming(chain, capsys):
    grill, firm, dec = chain
    rc = cli.main(["cognition", "breakdown", "Build the accounts API", "--no-firm"])
    assert rc == 0
    assert len(grill.calls) == 1
    assert len(firm.calls) == 0          # firming never invoked
    assert len(dec.calls) == 1
    out = capsys.readouterr().out
    assert "cognition_calls=2" in out
    # without firming, the seed done_when is empty — the spec still drives decompose
    assert "the accounts endpoints and their tests" in dec.calls[0]


def test_breakdown_json_emits_spec_chain_and_checklist(chain, capsys):
    rc = cli.main(["cognition", "breakdown", "Build the accounts API", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["cognition_calls"] == 3
    assert [s["stage"] for s in data["chain"]] == ["scope-grill", "firming", "decompose"]
    assert data["spec"] == _SPEC.strip()   # validate_step strips the finalized spec
    assert data["milestones"] == ["M1 — Accounts"]
    assert [i["id"] for i in data["checklist"]] == ["item1", "item2"]
    assert data["checklist"][1]["depends_on"] == ["item1"]


def test_breakdown_touches_no_registry_goalstore_or_statestore(chain, monkeypatch, capsys):
    """The cognition group is inspection-only: routed before any store opens
    (cli.py main()). Blow up if the CLI ever constructs one on this path."""
    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("breakdown must not construct registry/store")

    monkeypatch.setattr(cli, "ProjectRegistry", _boom)
    monkeypatch.setattr(cli, "GoalStore", _boom)
    monkeypatch.setattr(cli, "StateStore", _boom)
    rc = cli.main(["cognition", "breakdown", "Build the accounts API"])
    assert rc == 0
    assert "cognition_calls=3" in capsys.readouterr().out
