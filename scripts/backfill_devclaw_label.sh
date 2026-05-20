#!/usr/bin/env bash
# Retroactive `devclaw` label backfill.
#
# Walks the PR history of each repo the devclaw runner has historically opened
# PRs against, and applies the `devclaw` label to every PR whose head branch
# matches `^kit/`. Skips `codex/*` and any other non-Kit branches.
#
# Safe to re-run — `gh label create --force` is create-or-update and
# `gh pr edit --add-label` is a no-op when the label is already attached.
#
# Usage:
#   bash scripts/backfill_devclaw_label.sh
#
# Requires: gh (authenticated as the principal that owns the repos).

set -euo pipefail

REPOS=(
  dsdevq/devclaw
  dsdevq/lifekit-stack
  dsdevq/lifekit-dashboard
)

LABEL_COLOR="1f6feb"
LABEL_DESCRIPTION="Opened by the devclaw autonomous orchestrator. Branch pattern: kit/<task_id>-*. See ~/.life/projects/<project>/tasks/<id>/spec.yaml for the spec."

for repo in "${REPOS[@]}"; do
  echo "==> $repo"

  # 1. Ensure the label exists on the repo (idempotent).
  gh label create devclaw \
    --repo "$repo" \
    --color "$LABEL_COLOR" \
    --description "$LABEL_DESCRIPTION" \
    --force 2>/dev/null || true

  # 2. Enumerate every PR (open + closed + merged), filter to kit/* head
  #    branches, and add the label. gh prints PRs as `<num>\t<headBranch>`.
  gh pr list \
    --repo "$repo" \
    --state all \
    --limit 1000 \
    --json number,headRefName \
    --jq '.[] | "\(.number)\t\(.headRefName)"' \
  | while IFS=$'\t' read -r num branch; do
      case "$branch" in
        kit/*)
          echo "    PR #$num ($branch) → +devclaw"
          gh pr edit "$num" --repo "$repo" --add-label devclaw >/dev/null
          ;;
        *)
          # Explicitly skip codex/* and every other non-Kit branch.
          ;;
      esac
    done
done

echo "Done."
