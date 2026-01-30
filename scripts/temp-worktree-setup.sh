#!/bin/bash
# Worktree setup split into separate commands to avoid 2-minute timeout.
# Each step runs independently, getting its own timeout window.

set -e

SETUP_LOG="${HOME}/.issue-orchestrator/worktree-setup.log"
mkdir -p "$(dirname "$SETUP_LOG")"

echo "[START] temp-worktree-setup pid=$$ ts=$(date -Iseconds) pwd=$(pwd)" >> "$SETUP_LOG"

# Step 1: Create venv and install Python dependencies
echo "Step 1/3: Creating venv and installing Python dependencies..."
make venv

# Step 2: Install VS Code extension dependencies
echo "Step 2/3: Installing VS Code extension dependencies..."
(cd packages/vscode && npm install --silent)

# Step 3: Install Playwright browsers
echo "Step 3/3: Installing Playwright browsers..."
.venv/bin/playwright install chromium --with-deps 2>/dev/null || .venv/bin/playwright install chromium

echo "[END] temp-worktree-setup pid=$$ ts=$(date -Iseconds)" >> "$SETUP_LOG"
echo ""
echo "Worktree setup complete! Activate with: source .venv/bin/activate"
