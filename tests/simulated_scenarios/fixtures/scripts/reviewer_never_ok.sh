#!/usr/bin/env bash
set -euo pipefail

echo '{"response_type":"changes_requested","getting_closer":true,"response_text":"Needs more work"}' \
  > "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"
