# Verify and iterate

Keep the change focused — do not refactor unrelated code.

When done, VERIFY your work with the project's OWN tools, and iterate until they pass:

- Run the test/build command AND the linter, formatter, and type-checker if the repo has any (look in `package.json` scripts, `pyproject.toml` / `setup.cfg`, `Makefile`, `.pre-commit-config.yaml`, or configs like `.eslintrc` / `ruff` / `mypy` / `tsconfig`).
- Fix everything they flag, not only failing tests.

Finish with a short summary of what you changed and the checks you ran (tests + lint + types) to verify it.
