#!/usr/bin/env bash
set -euo pipefail

marker=".issue-orchestrator/review-attempt"
if [[ -f "$marker" ]]; then
  python -m issue_orchestrator.entrypoints.cli_tools.agent_done approved \
    --summary "Looks good now" \
    --risk low \
    --checks tests_added
  exit 0
fi

mkdir -p "$(dirname "$marker")"
echo "1" > "$marker"

python -m issue_orchestrator.entrypoints.cli_tools.agent_done changes_requested \
  --issues "Needs fixes before approval" \
  --risk medium
