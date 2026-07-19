# create-a-python-package-mathx-e6b789 — discovery brief

_generated 2026-07-19T16:27:15+00:00_

## Current state

The repository is essentially empty. It contains a single tracked file, `README.md`, whose entire content is `# mathx target` (confirmed by both the read-only analysis and REPOSITORY CONTEXT). There is one git commit (`7160f2e init`) on branch `main`, with a remote configured. No `AGENTS.md`, `CLAUDE.md`, `pyproject.toml`, `setup.py`, or any build/package manifest exists. No Python source files, no package directory, and no tests exist anywhere in the tree.

## Gap to good

- No `mathx` package exists at all — no `__init__.py`, no module files.
- `add()` and `mul()` functions are not implemented anywhere.
- No `tests/` directory or test file exists to import and exercise the functions.
- No packaging metadata (`pyproject.toml` or `setup.py`) to make `mathx` an installable/importable package.
- No `AGENTS.md` to record structure/conventions for future work — this is a from-scratch build, not an incremental one.

This is a 0%-built target: every artifact the owner asked for is missing, but the scope itself is small and well-defined.

## What good looks like

- `mathx/` package directory with an `__init__.py` (can be empty or re-export `add`/`mul` for convenience).
- `mathx/add.py` containing a clear `add(a, b)` function.
- `mathx/mul.py` containing a clear `mul(a, b)` function.
- `tests/test_mathx.py` (or similar) that imports both functions and asserts correct behavior on a few representative inputs.
- Minimal `pyproject.toml` so the package is installable/importable in a standard way (name `mathx`, basic build backend).
- A short `AGENTS.md` capturing the package layout and how to run the tests, so a future task doesn't have to re-derive it.
