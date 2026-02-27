#!/usr/bin/env bash
set -euo pipefail

# $1 is prompt file path (ignored)
# Write structured response to the file the orchestrator reads.
echo '{"response_type":"ok","getting_closer":true,"response_text":"LGTM"}' \
  > "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"
