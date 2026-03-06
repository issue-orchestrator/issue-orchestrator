#!/usr/bin/env bash
# Generate .mcp.json with a worktree-isolated Playwright user-data-dir.
#
# Each worktree (or the base repo) gets its own Chrome profile directory
# so multiple Claude Code sessions can run Playwright MCP concurrently
# without fighting over a shared browser instance.

set -euo pipefail

DIRNAME="$(basename "$(pwd)")"
USER_DATA_DIR="/tmp/playwright-mcp-${DIRNAME}"

cat > .mcp.json <<EOF
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": [
        "@playwright/mcp@latest",
        "--vision",
        "--user-data-dir", "${USER_DATA_DIR}"
      ]
    }
  }
}
EOF

echo "Generated .mcp.json (Playwright user-data-dir: ${USER_DATA_DIR})"
