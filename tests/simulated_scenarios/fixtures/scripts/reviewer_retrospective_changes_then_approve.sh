#!/usr/bin/env bash
set -euo pipefail

session_id="${ISSUE_ORCHESTRATOR_SESSION_ID:-}"

if [[ "$session_id" == retrospective-review-* ]]; then
  python -m issue_orchestrator.entrypoints.cli_tools.reviewer_done changes_requested \
    --issues "Retrospective audit found stale implementation; rerun coder rework" \
    --risk medium
  exit 0
fi

python -m issue_orchestrator.entrypoints.cli_tools.reviewer_done approved \
  --summary "Rework PR looks good" \
  --risk low \
  --checks tests_added
