#!/usr/bin/env bash
set -euo pipefail

python -m issue_orchestrator.entrypoints.cli_tools.agent_done needs_human \
  --question "Simulated scenario needs human input"
