#!/usr/bin/env bash
set -euo pipefail

# Read JSON context from stdin (required by contract).
# The validator can parse this to get run_dir, repo_root, agent_label, etc.
# Example: python - <<'PY' ... JSON.parse ...
_context_json="$(cat)"

# Run repository validation.
make validate
