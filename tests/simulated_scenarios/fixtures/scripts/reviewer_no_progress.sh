#!/usr/bin/env bash
set -euo pipefail

echo '{"response_type":"changes_requested","getting_closer":false,"response_text":"No progress"}' \
  > "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"
