#!/usr/bin/env bash
set -euo pipefail

python -m issue_orchestrator.entrypoints.cli_tools.agent_done completed \
  --implementation "Simulated scenario completed" \
  --problems "None"
