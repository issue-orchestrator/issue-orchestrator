#!/usr/bin/env bash
set -euo pipefail

python -m issue_orchestrator.entrypoints.cli_tools.reviewer_done approved \
  --summary "Looks good" \
  --risk low \
  --checks tests_added
