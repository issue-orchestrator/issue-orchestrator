#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_write_completion.sh"
write_completion blocked "Simulated scenario blocked" "Tried the main path"
