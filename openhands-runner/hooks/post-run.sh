#!/usr/bin/env bash
# post-run hook — invoked by runner.py after the agent finishes, BEFORE the
# verify gate.
#
# Args: $1 workspace_dir   $2 kind   $3 task_id   $4 verify_cmd (may be empty)
#
# Best-effort mechanical checks. Writes warnings to stdout (the runner captures
# them and attaches to the result). Failures here do NOT abort — the verify
# gate is the source of truth for go/no-go.

set -u

workspace_dir="${1:-}"
kind="${2:-implement_feature}"
verify_cmd="${4:-}"

[ -d "$workspace_dir" ] || exit 0

# ---- check 1: new browser tests added but verify_cmd doesn't run them ----
# Catches the cf-11 failure mode (Playwright tests committed but the gate
# stayed pytest-only). Looks at the diff against pre-run HEAD; falls back to
# scanning the worktree if the snapshot is missing.
pre_head="$(cat "$workspace_dir/.devclaw-pre-head" 2>/dev/null || true)"
new_spec_files=""
if [ -n "$pre_head" ] && [ -d "$workspace_dir/.git" ]; then
  new_spec_files=$(git -C "$workspace_dir" diff --name-only --diff-filter=A "$pre_head" -- \
    '**/*.spec.ts' '**/*.spec.js' '**/*.spec.tsx' 'e2e/**' 'tests/e2e/**' 2>/dev/null || true)
fi
if [ -n "$new_spec_files" ]; then
  if [ -n "$verify_cmd" ] && ! echo "$verify_cmd" | grep -qiE 'playwright|pytest-playwright'; then
    echo "warn: new browser tests added but verify_cmd does not run them:"
    echo "$new_spec_files" | sed 's/^/  - /'
    echo "  verify_cmd: $verify_cmd"
    echo "  fix: extend verify_cmd to include 'npx playwright test' (or equivalent)."
  fi
fi

# ---- check 1b: web-UI SOURCE changed but the gate runs no browser E2E ----
# Stronger than 1a (which only fires on ADDED spec files): a change that edits
# components/templates without any browser run will fail the host browser-gate
# CLOSED at settle. Surface it EARLY so the agent adds the spec + JSON reporter
# now, instead of discovering it after a wasted gate round.
changed_ui=""
if [ -n "$pre_head" ] && [ -d "$workspace_dir/.git" ]; then
  changed_ui=$(git -C "$workspace_dir" diff --name-only "$pre_head" -- \
    '**/*.component.ts' '**/*.component.html' '*/src/app/**' 'angular.json' 2>/dev/null || true)
fi
if [ -n "$changed_ui" ] && [ -n "$verify_cmd" ] && ! echo "$verify_cmd" | grep -qiE 'playwright'; then
  echo "warn: web-UI source changed but verify_cmd runs no browser E2E — the browser gate will fail this CLOSED:"
  echo "$changed_ui" | sed 's/^/  - /'
  echo "  verify_cmd: $verify_cmd"
  echo "  fix: add a Playwright spec that exercises this change in the running app, and extend verify_cmd to run 'npx playwright test --reporter=json' (see craft/playwright.md)."
fi

# ---- check 2: AGENTS.md exists but wasn't updated this run ----
# The _common skill tells the agent to keep AGENTS.md current. If it shipped a
# change without touching AGENTS.md, surface a soft warning so the next planner
# tick sees it.
if [ -f "$workspace_dir/AGENTS.md" ] && [ -n "$pre_head" ] && [ -d "$workspace_dir/.git" ]; then
  if ! git -C "$workspace_dir" diff --name-only "$pre_head" -- AGENTS.md 2>/dev/null | grep -q AGENTS.md; then
    # Only warn if the run actually changed something else (i.e. AGENTS.md was
    # the only file untouched).
    other_changes=$(git -C "$workspace_dir" diff --name-only "$pre_head" 2>/dev/null | grep -v '^AGENTS.md$' | head -1 || true)
    if [ -n "$other_changes" ]; then
      echo "warn: AGENTS.md exists but was not updated this run; future agents may re-derive what you learned."
    fi
  fi
fi

# Clean up the pre-run snapshot.
rm -f "$workspace_dir/.devclaw-pre-head" 2>/dev/null || true

exit 0
