# create-a-python-package-mathx-79bb78 — discovery brief

_generated 2026-07-19T16:20:43+00:00_

## Current state

The repository is essentially empty. It contains only a `README.md` file (2 lines, reading `# mathx target`) and standard git scaffolding — confirmed by both the read-only agent's exploration and the REPOSITORY CONTEXT (`tracked_top_level: README.md`). There is no Python source code, no `mathx` package, no functions, no tests, and no packaging metadata (`pyproject.toml`, `setup.py` are both confirmed missing). No `AGENTS.md` or `CLAUDE.md` exists either. The repo is on branch `main` at a single init commit.

## Gap to good

Everything the owner asked for still needs to be built — this is a 0% starting point, not a refinement:

- No `mathx` package exists at all (no directory, no `__init__.py`)
- `add()` and `mul()` functions don't exist anywhere, let alone in separate modules
- No `tests/` file, so there's nothing importing or exercising either function
- No packaging file (`pyproject.toml`/`setup.py`), so even once written, the package isn't installable or import-clean by standard means
- No `AGENTS.md` to record conventions for future work on this repo

## What good looks like

- `mathx/` package directory with `__init__.py`
- `mathx/add.py` containing `add()`, `mathx/mul.py` containing `mul()` — each function in its own module as requested
- `tests/test_mathx.py` (or similar) that imports both `add` and `mul` and asserts on their behavior, including a basic edge case each (e.g. negative numbers, zero)
- A minimal `pyproject.toml` declaring package metadata so it's installable (`pip install -e .`) and the test command is discoverable
- A short `AGENTS.md` noting the stack (pure Python, no deps) and how to run tests, so future changes don't re-derive this from scratch
