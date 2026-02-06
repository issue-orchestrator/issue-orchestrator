#!/usr/bin/env bash
set -euo pipefail

python -m issue_orchestrator.entrypoints.cli_tools.agent_done blocked \
  --reason "Simulated scenario blocked" \
  --attempted "Tried the main path"
