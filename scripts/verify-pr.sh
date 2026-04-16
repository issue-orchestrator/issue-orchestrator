#!/usr/bin/env bash
set -euo pipefail

# Managed by issue-orchestrator harden-repo: verify-pr

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

validation_cmd='make validate'

if ! git diff --quiet --exit-code -- . || ! git diff --cached --quiet --exit-code -- .; then
  echo >&2 "verify-pr: requires a clean tracked worktree."
  echo >&2 "Commit or stash tracked changes, then rerun scripts/verify-pr.sh."
  exit 1
fi

echo "verify-pr: running $validation_cmd"
bash -lc "$validation_cmd"
