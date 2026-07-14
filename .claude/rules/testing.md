# Testing — how to run and write tests in this repo

The suite is **fully stubbed** — no docker, no `claude` binary, ~1226 tests in
~55s. Anything needing real docker/claude is an integration concern:
`/live-shakedown`, never pytest.

## Running

- Always run with a private tmpdir: `TMPDIR=$(mktemp -d) .venv/bin/python -m pytest -q`.
  `/tmp/pytest-of-<user>` can be root-owned on this host (a past root-run
  pytest), which crashes every `tmp_path` fixture with the default basetemp.
- **In a git worktree, verify the import path FIRST**:
  `.venv/bin/python -c "import devclaw; print(devclaw.__file__)"` must print the
  WORKTREE path. The shared venv's editable install is a `.pth` pointing at the
  main checkout; `python -m pytest` from the worktree root wins only because cwd
  precedes site-packages — run from anywhere else and you silently test the
  wrong code.
- Run the full suite before opening any PR. Green baseline lives in the most
  recent PR descriptions; a lower count than baseline means you broke something
  even if your own tests pass.

## Writing

- **Every behavior-change PR ships a named regression test** — named after the
  behavior, not the function (`test_resume_goal_unblocks_without_steering_and_replans_next_tick`).
- Fixture map: `tests/goal_fakes.py` has `FakeClaude` (its `.calls` count IS the
  zero-token quota assertion), `FakeEngine`, `RecordingNotifier`, `seed_goal`.
  Goal-tick behavior → `tests/test_goal_tick.py`; transitions/CAS in isolation →
  `test_goal_transitions.py`; store row/view behavior → `test_goal_store*.py`;
  queue/gate → `test_review_gate*.py`, `test_task_retry.py`.
- Tests that build a "realistic repo" fixture copy the shape in
  `tests/test_review_gate.py` (real `git init` + .NET/Angular marker files) —
  don't invent a new fixture style.
- Zero-token guard tests (`FakeClaude.calls == 0` on idle/blocked paths) are
  load-bearing. If your change makes one fail, the change is wrong — never the
  test.
- Prompt-content tests assert both presence AND absence; when asserting a
  marker is absent from a prompt, first prove it's absent from the raw template
  (a template example like `Program.cs` is a canned prior, not grounding).
