#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_write_completion.sh"
write_completion completed "Applied changes" "None"
# $1 is prompt file path (ignored)
echo '{"response_type":"ok","response_text":"Applied changes"}'
