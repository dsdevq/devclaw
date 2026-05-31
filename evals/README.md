# DevClaw evals — golden-project build harness

A repeatable, scored smoke test for build-a-project-from-scratch. It drives the
**real** pipeline (claude + docker) through `build_project → grill → approve →
build`, grades each run against a hard acceptance check, and rolls N runs into a
pass-rate. Because the build is non-deterministic, **one run means little — the
rate across N runs is the metric**, and comparing the rate across git SHAs is how
you tell whether a change actually improved things.

This is the project's answer to the long-standing open question *"does OpenHands
have the skill-quality for high-quality autonomous work?"* — now measurable.

## How it's isolated
The grill is answered from a **fixed script** (`answers.txt`), so the spec is held
roughly constant and you're measuring the **build**, not the interview. (Evaluating
the grill itself is a separate concern.)

## Run it

```bash
# 1. real engine up (see ../docs/live-shakedown.md for setup):
docker build -t devclaw-sandbox:latest -f .sandcastle/Dockerfile .
DEVCLAW_TRANSPORT=http DEVCLAW_PORT=8000 devclaw-mcp     # leave running

# 2. run the eval (start SMALL — each run is real Pro spend + minutes):
python evals/run.py evals/json-yaml-cli --n 3
```

Output per run + an aggregate:

```
=== SUMMARY ===
{ "runs": 3, "acceptance_passed": 2, "acceptance_pass_rate": 0.667,
  "builds_completed": 3, "avg_milestone_pct": 83.3, "avg_wall_ms": 412000,
  "stuck_runs": 0, "git_sha": "abc1234", "project": "json-yaml-cli" }
```

Artifacts land in `evals/runs/<git-sha>/<project>/` (per-run scorecard + the agreed
spec + summary). `evals/runs/` is gitignored — it's results, not source.

## The success criterion
For `json-yaml-cli`: **acceptance** = `python -m jyq` round-trips JSON → YAML → JSON
losslessly (`accept.sh`). A healthy result is a high `acceptance_pass_rate`. As you
polish the feature, the rate at a new SHA should climb vs. the old one — that's
"progress," made objective.

## Scorecard fields
`acceptance_passed` (the gate) · `program_status` · `tasks_done/total` ·
`milestone_done/total` + `milestone_pct` (partial credit at milestone granularity) ·
`wall_ms` · `stuck` (no progress within `--stuck`). Aggregated by `devclaw/evals.py`,
which is unit-tested (`tests/test_evals.py`) so the *scoring* is trustworthy even
though the live runs aren't reproducible.

## Failure analysis (`--judge`)

Add `--judge` to automate the "what went wrong?" step. After each run a second
`claude` call reads the spec, the task DAG, an event digest, and the acceptance
result, then buckets the run into a **fixed failure-mode vocabulary** — so failures
*aggregate*:

```bash
python evals/run.py evals/json-yaml-cli --n 5 --judge
```

```
=== SUMMARY ===
{ … "failure_analysis": {
      "runs_judged": 5,
      "by_category": {"success": 3, "acceptance_gap": 1, "engine_failure": 1},
      "top_failure_mode": "acceptance_gap" } }
```

Categories: `success · planning_error · incomplete_build · constraint_violation ·
acceptance_gap · engine_failure · stuck · other`. Each run's verdict (category +
diagnosis + a concrete suggestion) is saved in its `run-*.json`. `top_failure_mode`
tells you where to spend the next polish pass. The judge scoring/vocab is unit-tested
(`tests/test_eval_judge.py`); the diagnosis text is `claude`'s.

## Add a project
Create `evals/<slug>/` with:
- `idea.txt` — the `build_project` idea (pin the interface contract so acceptance is well-defined)
- `answers.txt` — one scripted grill answer per line (extras default to "use your recommendation")
- `accept.sh` — runs in the built workspace; exit 0 = pass

## Tuning
`--n` runs · `--timeout` per-run wall cap (s) · `--stuck` no-progress timeout (s) ·
`--url` the server's `/mcp` · `--out` archive dir.
