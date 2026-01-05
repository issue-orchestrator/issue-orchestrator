#!/bin/bash
# export_source.sh - Create a clean zip of the project for external review
#
# Excludes: caches, virtualenvs, secrets, git history, build artifacts, IDE files
# Output: Creates zip as sibling of project directory (../issue-orchestrator-source-*.zip)

set -e

PROJECT_NAME="issue-orchestrator"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
OUTPUT_FILE="${PROJECT_NAME}-source-${TIMESTAMP}.zip"

# Change to project root (parent of scripts directory)
cd "$(dirname "$0")/.."

# Create zip as peer of project directory
OUTPUT_PATH="../${OUTPUT_FILE}"

echo "Creating clean source export: ${OUTPUT_PATH}"

zip -r "${OUTPUT_PATH}" . \
    -x "*.pyc" \
    -x "*__pycache__*" \
    -x "*.pyo" \
    -x "*.pyd" \
    -x ".venv/*" \
    -x "venv/*" \
    -x "env/*" \
    -x ".env" \
    -x ".env.*" \
    -x "*.env" \
    -x ".git/*" \
    -x ".gitignore" \
    -x "dist/*" \
    -x "build/*" \
    -x "*.egg-info/*" \
    -x ".eggs/*" \
    -x ".pytest_cache/*" \
    -x ".mypy_cache/*" \
    -x ".ruff_cache/*" \
    -x ".coverage" \
    -x "htmlcov/*" \
    -x "coverage.xml" \
    -x ".tox/*" \
    -x ".nox/*" \
    -x "*.so" \
    -x "*.dylib" \
    -x ".idea/*" \
    -x ".vscode/*" \
    -x "*.swp" \
    -x "*.swo" \
    -x "*~" \
    -x ".DS_Store" \
    -x "Thumbs.db" \
    -x "node_modules/*" \
    -x "*.log" \
    -x "*.zip" \
    -x ".issue-orchestrator/*" \
    -x "*.sqlite" \
    -x "*.db"

# Show what was created
SIZE=$(du -h "${OUTPUT_PATH}" | cut -f1)
echo ""
echo "Created: $(cd .. && pwd)/${OUTPUT_FILE} (${SIZE})"
echo ""
echo "Contents preview:"
unzip -l "${OUTPUT_PATH}" | head -30
echo "..."
echo ""
FILE_COUNT=$(unzip -l "${OUTPUT_PATH}" | tail -1 | awk '{print $2}')
echo "Total: ${FILE_COUNT} files"
