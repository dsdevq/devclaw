"""docs/reference/env-vars.md ↔ code parity — the env surface stays honest.

The doc calls itself the single source of truth; history shows it drifts
(pre-2026-07-11 it documented ~60 of ~85 vars actually read). This test makes
drift a failure instead of an archaeology project:

  - every ``DEVCLAW_*`` var the runtime reads must have a row in the doc;
  - every documented row must correspond to a real read in the code.

Scope is the runtime: the ``devclaw`` package + the in-sandbox runner. Test
fixtures (``DEVCLAW_TEST_*`` in tests/), the offline eval harness
(``MEASURE_*`` in evals/), and Claude-harness hooks (.claude/) are not env
surface and are deliberately outside the scan.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DOC = _REPO / "docs" / "reference" / "env-vars.md"

#: an env READ: os.environ.get / [] / setdefault / os.getenv, tolerating a
#: line break between the call and the var-name literal.
_READ_RE = re.compile(
    r'(?:environ(?:\.get|\.setdefault)?|getenv)\s*[\(\[]\s*"(DEVCLAW_[A-Z_]+)"'
)
_ROW_RE = re.compile(r"^\| `(DEVCLAW_[A-Z_]+)`", re.MULTILINE)


def _runtime_reads() -> set[str]:
    files = list((_REPO / "devclaw").rglob("*.py"))
    files += list((_REPO / "openhands-runner").glob("*.py"))
    assert files, "runtime source not found — repo layout changed?"
    reads: set[str] = set()
    for f in files:
        reads |= set(_READ_RE.findall(f.read_text(encoding="utf-8")))
    return reads


def _documented_rows() -> set[str]:
    return set(_ROW_RE.findall(_DOC.read_text(encoding="utf-8")))


def test_every_env_read_is_documented():
    undocumented = _runtime_reads() - _documented_rows()
    assert not undocumented, (
        f"env vars read by the runtime but missing from docs/reference/env-vars.md: "
        f"{sorted(undocumented)} — add a row (or demote the read to a "
        f"constant if it isn't a real per-host fact)"
    )


def test_every_documented_var_is_read():
    ghosts = _documented_rows() - _runtime_reads()
    assert not ghosts, (
        f"vars documented in docs/reference/env-vars.md but read nowhere in the "
        f"runtime: {sorted(ghosts)} — remove the row (dead config docs are "
        f"worse than none)"
    )
