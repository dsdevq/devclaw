# Verify and iterate

Keep the change focused. Refactoring **what you touch** is part of the change — if you edit a god object to add a feature, splitting it is the work, not unrelated. The line is between refactors that _support_ the change (in scope) and refactors of code you didn't otherwise need to touch (out of scope).

When done, VERIFY your work with the project's OWN tools, and iterate until they pass:

- Run the test/build command AND the linter, formatter, and type-checker if the repo has any (look in `package.json` scripts, `pyproject.toml` / `setup.cfg`, `Makefile`, `.pre-commit-config.yaml`, or configs like `.eslintrc` / `ruff` / `mypy` / `tsconfig`).
- Fix everything they flag, not only failing tests.

Finish with a short summary of what you changed and the checks you ran (tests + lint + types) to verify it.
