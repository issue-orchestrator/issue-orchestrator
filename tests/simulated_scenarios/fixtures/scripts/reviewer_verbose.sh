#!/usr/bin/env bash
set -euo pipefail

# Verbose reviewer script with ANSI codes, spinners, and marker strings
# for testing that ui-session.log filtering works end-to-end.

echo -e "\x1b[36m[reviewer] Analyzing code changes\x1b[0m"
echo "···"
echo "Reviewing file: src/auth.py"
echo "Review verdict: code-quality-approved"

echo '{"response_type":"ok","getting_closer":true,"response_text":"LGTM - code quality approved"}' \
  > "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"
