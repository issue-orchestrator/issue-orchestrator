#!/usr/bin/env bash
# Fix AGENTS.md and CLAUDE.md symlink issues
#
# Fixes:
# 1. CLAUDE.md that is not a symlink -> move to AGENTS.md, create symlink
#    (fails if AGENTS.md already exists)
# 2. AGENTS.md without CLAUDE.md symlink -> create CLAUDE.md symlink
#    (fails if CLAUDE.md exists but is not a symlink to AGENTS.md)
#
# Exit codes:
#   0 - All fixes applied (or nothing to fix)
#   1 - Errors encountered (conflicts that need manual resolution)

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

errors=0
fixes=0

# Get repo root
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

echo "Fixing AGENTS.md/CLAUDE.md conventions..."
echo

# Fix 1: CLAUDE.md files that are not symlinks (excluding .claude/)
echo "Checking for CLAUDE.md files to convert..."
while IFS= read -r claude_file; do
    # Skip files in .claude directory
    if [[ "$claude_file" == ./.claude/* ]]; then
        continue
    fi

    if [[ ! -L "$claude_file" ]]; then
        dir=$(dirname "$claude_file")
        agents_file="$dir/AGENTS.md"

        if [[ -e "$agents_file" ]]; then
            echo -e "${RED}ERROR:${NC} Cannot fix $claude_file"
            echo "       $agents_file already exists. Manual resolution required:"
            echo "       - Merge contents of both files into AGENTS.md"
            echo "       - Remove CLAUDE.md"
            echo "       - Create symlink: ln -s AGENTS.md CLAUDE.md"
            ((errors++))
        else
            echo -e "${YELLOW}FIX:${NC} Converting $claude_file to AGENTS.md with symlink"
            mv "$claude_file" "$agents_file"
            ln -s AGENTS.md "$claude_file"
            ((fixes++))
        fi
    fi
done < <(find . -name "CLAUDE.md" -not -path "./.git/*")

# Fix 2: AGENTS.md files without corresponding CLAUDE.md symlinks
echo "Checking for AGENTS.md files missing CLAUDE.md symlinks..."
while IFS= read -r agents_file; do
    dir=$(dirname "$agents_file")
    claude_file="$dir/CLAUDE.md"

    if [[ ! -e "$claude_file" ]]; then
        echo -e "${YELLOW}FIX:${NC} Creating symlink $claude_file -> AGENTS.md"
        ln -s AGENTS.md "$claude_file"
        ((fixes++))
    elif [[ ! -L "$claude_file" ]]; then
        # This should have been caught in Fix 1, but handle edge cases
        echo -e "${RED}ERROR:${NC} $claude_file exists but is not a symlink"
        echo "       Manual resolution required (see above)"
        ((errors++))
    else
        # Check if symlink points to correct target
        target=$(readlink "$claude_file")
        if [[ "$target" != "AGENTS.md" ]]; then
            echo -e "${YELLOW}FIX:${NC} Updating symlink $claude_file to point to AGENTS.md (was: $target)"
            rm "$claude_file"
            ln -s AGENTS.md "$claude_file"
            ((fixes++))
        fi
    fi
done < <(find . -name "AGENTS.md" -not -path "./.git/*" -not -path "./.claude/*")

echo
if [[ $errors -gt 0 ]]; then
    echo -e "${RED}Encountered $errors error(s) requiring manual resolution${NC}"
    echo "Applied $fixes fix(es)"
    exit 1
elif [[ $fixes -gt 0 ]]; then
    echo -e "${GREEN}Applied $fixes fix(es) successfully${NC}"
    exit 0
else
    echo -e "${GREEN}Nothing to fix - all conventions already satisfied${NC}"
    exit 0
fi
