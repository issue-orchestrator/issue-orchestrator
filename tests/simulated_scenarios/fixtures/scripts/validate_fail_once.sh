#!/usr/bin/env bash
set -euo pipefail

marker="${1:?per-scenario validation state path is required}"
if [[ -f "$marker" ]]; then
  echo "validation ok"
  exit 0
fi

mkdir -p "$(dirname "$marker")"
echo "first failure" > "$marker"
echo "validation failed once" >&2
exit 1
