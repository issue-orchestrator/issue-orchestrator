#!/usr/bin/env bash
set -euo pipefail

python -m issue_orchestrator.entrypoints.cli_tools.agent_done changes_requested \
  --issues "Needs fixes before approval" \
  --risk medium
