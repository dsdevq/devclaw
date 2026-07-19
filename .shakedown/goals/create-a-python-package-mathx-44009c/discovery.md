# create-a-python-package-mathx-44009c — discovery brief

_generated 2026-07-19T16:22:34+00:00_

## Current state

The repository is essentially empty. It contains a single commit (`7160f2e init`) on branch `main`, with only a two-line `README.md` reading "# mathx target" and an `.mcp.json` tooling config file. There is no `mathx` package, no `add()` or `mul()` function, no `tests/` directory, and no packaging file (`pyproject.toml` and `setup.py` are both confirmed missing). No `AGENTS.md` or `CLAUDE.md` exists either, so there's no accumulated project guidance to follow. This is a blank slate with no legacy code or conflicting decisions to work around.

## Gap to good

Everything the owner asked for is missing:
- No `mathx` package directory (no `__init__.py`, no module files)
- No `add()` function, no dedicated module for it
- No `mul()` function, no dedicated module for it
- No `tests/` directory or test file importing both functions
- No packaging metadata (`pyproject.toml`/`setup.py`), so the package isn't installable
- No README usage/install instructions beyond the placeholder title

This isn't a matter of fixing or extending existing code — it's a from-scratch build.

## What good looks like

- `mathx/` package with `__init__.py` (and re-exports of `add`/`mul` for a clean public API)
- `mathx/add.py` containing `def add(a, b): return a + b`
- `mathx/mul.py` containing `def mul(a, b): return a * b`
- `tests/test_mathx.py` that imports both `add` and `mul` and asserts correct behavior on a few cases (including edge cases like negatives/zero)
- `pyproject.toml` declaring the package so it's installable (`pip install -e .`) and test-runnable (e.g. via `pytest`)
- A README with a one-line description and a minimal usage example
- `AGENTS.md` capturing the package layout and how to run tests, so future changes don't re-derive this from scratch
