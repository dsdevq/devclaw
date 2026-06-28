#!/usr/bin/env bash
# pre-run hook — invoked by runner.py before the agent starts.
#
# Args: $1 workspace_dir   $2 kind   $3 task_id
#
# Best-effort. Failures here surface as warnings in the runner result but do
# NOT abort the run. Keep checks fast (< 1s total).

set -u

workspace_dir="${1:-}"
kind="${2:-implement_feature}"

# Sanity: workspace must exist and be writable for code-writing kinds.
if [ ! -d "$workspace_dir" ]; then
  echo "warn: workspace_dir does not exist: $workspace_dir"
fi
if [ "$kind" = "implement_feature" ] || [ "$kind" = "fix_bug" ] || [ "$kind" = "onboard" ]; then
  if [ ! -w "$workspace_dir" ]; then
    echo "warn: workspace_dir is not writable: $workspace_dir"
  fi
fi

# Snapshot the HEAD ref so post-run can tell what the agent changed.
if [ -d "$workspace_dir/.git" ]; then
  git -C "$workspace_dir" rev-parse HEAD 2>/dev/null > "$workspace_dir/.devclaw-pre-head" || true
fi

exit 0
