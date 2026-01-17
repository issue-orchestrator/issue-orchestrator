#!/usr/bin/env bash
# Guardrail: Validate AGENTS.md and CLAUDE.md symlink conventions
#
# Rules:
# 1. Every AGENTS.md must have a corresponding CLAUDE.md symlink pointing to it
# 2. Every CLAUDE.md (outside .claude/) must be a symlink to AGENTS.md
#
# Exit codes:
#   0 - All checks pass
#   1 - Violations found

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

errors=0

# Get repo root
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

echo "Checking AGENTS.md/CLAUDE.md conventions..."
echo

# Check 1: Find AGENTS.md files without corresponding CLAUDE.md symlinks
echo "Checking for AGENTS.md without CLAUDE.md symlinks..."
while IFS= read -r agents_file; do
    dir=$(dirname "$agents_file")
    claude_file="$dir/CLAUDE.md"

    if [[ ! -e "$claude_file" ]]; then
        echo -e "${RED}ERROR:${NC} $agents_file has no corresponding CLAUDE.md"
        ((errors++))
    elif [[ ! -L "$claude_file" ]]; then
        echo -e "${RED}ERROR:${NC} $claude_file exists but is not a symlink"
        ((errors++))
    else
        # Check if symlink points to AGENTS.md
        target=$(readlink "$claude_file")
        if [[ "$target" != "AGENTS.md" ]]; then
            echo -e "${RED}ERROR:${NC} $claude_file is a symlink but points to '$target' instead of 'AGENTS.md'"
            ((errors++))
        fi
    fi
done < <(find . -name "AGENTS.md" -not -path "./.git/*" -not -path "./.claude/*")

# Check 2: Find CLAUDE.md files that are not symlinks (excluding .claude/)
echo "Checking for CLAUDE.md files that are not symlinks..."
while IFS= read -r claude_file; do
    # Skip files in .claude directory
    if [[ "$claude_file" == ./.claude/* ]]; then
        continue
    fi

    if [[ ! -L "$claude_file" ]]; then
        echo -e "${RED}ERROR:${NC} $claude_file is not a symlink (should symlink to AGENTS.md)"
        ((errors++))
    fi
done < <(find . -name "CLAUDE.md" -not -path "./.git/*")

echo
if [[ $errors -eq 0 ]]; then
    echo -e "${GREEN}All checks passed!${NC}"
    exit 0
else
    echo -e "${RED}Found $errors error(s)${NC}"
    echo "Run 'scripts/fix_agents_md.sh' to fix these issues"
    exit 1
fi
