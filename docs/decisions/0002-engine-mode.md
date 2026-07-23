# Engine decision — OpenHands vs Claude-SDK

> **Decision record** — frozen as **ADR 0002** on 2026-07-13. The decision and the
> switch procedure below stand; point-in-time system references are not maintained
> for drift — the current system is [`../architecture.md`](../architecture.md).

## Current default

**OpenHands** (`run_sandcastle` via `openhands-runner/runner.py`). This is what production runs. Don't change without data.

## How to switch (without data)

```bash
DEVCLAW_ENGINE=claude_sdk devclaw-mcp
```

That's a per-process opt-in — the OpenHands path is unchanged.

## How to decide (with data)

Run the side-by-side spike on a host with `claude` + `docker` + the
`devclaw-sandbox:latest` image:

```bash
.venv/bin/python evals/compare_engines.py \
    --workspace /tmp/spike-ws \
    --repo git@github.com:lifekit-hq/lifekit-dashboard.git \
    --suite-file evals/passrate_suite.tsv
```

(The suite file is the same task list `measure_passrate.py` exercises. If it
doesn't exist yet, extract it from that driver.)

The script writes `evals/runs/compare-engines-<stamp>.json` with per-task
results for both engines and prints a one-line summary (pass rate, mean
wall-clock, error count) per engine.

## Decision rule

Switch to `claude_sdk` only if **all** of:

1. **Pass rate** — Claude-SDK ≥ OpenHands on the same suite (5/5 today).
2. **Mean wall-clock** — within ~20% of OpenHands. Slower is acceptable if
   the gap is small; 2x slower is not.
3. **Error rate** — exception/crash count ≤ OpenHands.
4. **Verified by hand** — at least one diff hand-reviewed per engine to
   catch silent-success / silent-gutting that the gate may not.

If any one fails, keep OpenHands and re-run after fixing the gap.

## If Claude-SDK wins

Then task #8 fires:

- delete `openhands-runner/`
- drop `openhands-sdk==1.24.0` from the sandbox Dockerfile + requirements
- make `claude_sdk` the unset-engine default in `server/_state.py`
- delete `devclaw/engine/sandcastle.py` if Claude-SDK fully replaces it
  (note: it currently provides `_build_claude_mounts`, `_strip_api_keys`,
  `_teardown`, `_translate_workspace_path` — the Claude-SDK engine reuses
  them today; move them somewhere shared first)
- update README's Status section + the architecture-v2 doc accordingly

Until the comparison runs, this stays at draft.

## Why not commit to switching now

Without the live comparison, the spike is a hypothesis: "fewer lines should
work as well." That hypothesis can be wrong in subtle ways:

- OpenHands' agent loop has stuck detection + retry heuristics tuned for
  long agentic runs. `claude --print` is one shot.
- OpenHands carries an event taxonomy the dashboard understands; the SDK
  path emits flat `StdoutLine` events.
- OpenHands' tool budget is per-iteration; `claude --print` has the
  session's own budget which is opaque from the outside.

Some of these may matter, some may not. The point of the spike is to
find out — not to assume.
