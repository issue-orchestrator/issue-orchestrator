#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR:-}" ]]; then
  mkdir -p "$ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR"
  cat > "$ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR/validation-record.json" <<'JSON'
{"passed": true}
JSON
fi

echo '{"response_type":"ok","getting_closer":true,"response_text":"Validation recorded"}'
