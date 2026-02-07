#!/usr/bin/env bash
set -euo pipefail

prompt_path="${1:-}"
if [[ -n "$prompt_path" && -f "$prompt_path" ]]; then
  echo "NOT_JSON"
  exit 0
fi

python -m issue_orchestrator.entrypoints.cli_tools.agent_done completed \
  --implementation "Simulated scenario completed" \
  --problems "None"
