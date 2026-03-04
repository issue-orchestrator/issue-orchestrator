#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_write_completion.sh"
write_completion needs_human "Simulated scenario needs human input"
