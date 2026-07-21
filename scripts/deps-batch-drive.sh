#!/usr/bin/env bash
set -euo pipefail

# deps-batch-drive.sh — deterministic spine for the weekly dependency batch.
#
# Creates a fresh worktree off origin/main, runs `make deps-batch` (upgrade +
# full local verify via validate-pr-raw), then either stops for you to review
# the diff (default) or opens the PR (--pr). On a verification failure it stops
# with the worktree intact so you — or the /deps-batch agent command — can fix
# forward, then re-run `make deps-batch` and commit.
#
# This is the human-maintainer / interactive path. An orchestrated coding agent
# must NOT run the --pr path: hooks block agent push/PR; it completes via
# coding-done instead.
#
# Policy lives in .claude/skills/dependency-upgrades/SKILL.md — fix forward, no
# pins, security never defers. Read it before overriding anything here.
#
# Note on cost: with --pr the full suite runs twice — once inside deps-batch
# (pre-commit, dirty tree) and once from the pre-push hook after commit. That
# double-run is inherent to the SHA-keyed validation cache (nothing to seed
# before the commit exists); it is the accepted price of a verified push.

usage() {
  cat <<'EOF'
Usage: scripts/deps-batch-drive.sh [options]

  --branch NAME   Worktree branch name (default: deps-YYYY-MM)
  --no-major      npm stays within existing ^/~ ranges. (Python still gets
                  majors regardless — its pyproject constraints are unbounded
                  floors, so `uv lock --upgrade` already crosses majors.)
  --major         Cross npm majors too, via npm-check-updates (default).
  --pr            On green: commit --signoff, push, and open the PR.
                  Omit to stop after verify and print the diff + next steps.
  -h, --help      Show this help.

Default: --major, no --pr (upgrade + verify, then stop for human review).
EOF
}

MAJOR=1
DO_PR=0
BRANCH=""

while [ $# -gt 0 ]; do
  case "$1" in
    --branch) BRANCH="${2:?--branch needs a value}"; shift 2 ;;
    --no-major) MAJOR=0; shift ;;
    --major) MAJOR=1; shift ;;
    --pr) DO_PR=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "deps-batch-drive: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
parent="$(dirname "$repo_root")"

if [ -z "$BRANCH" ]; then
  BRANCH="deps-$(date +%Y-%m)"
fi
wt="$parent/issue-orchestrator-wt-$BRANCH"

if [ -e "$wt" ]; then
  echo "deps-batch-drive: worktree path already exists: $wt" >&2
  echo "Remove it or pass --branch NAME for a fresh one." >&2
  exit 1
fi
if git -C "$repo_root" show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "deps-batch-drive: branch already exists: $BRANCH" >&2
  echo "Pick another with --branch NAME (or delete the stale branch)." >&2
  exit 1
fi

echo "==> Fetching origin/main..."
git -C "$repo_root" fetch origin main

echo "==> Creating worktree off origin/main (branch $BRANCH):"
echo "    $wt"
git -C "$repo_root" worktree add "$wt" -b "$BRANCH" origin/main

cd "$wt"

echo "==> make worktree-setup..."
make worktree-setup

echo "==> make deps-batch (MAJOR=$MAJOR)..."
# The deps-batch target only accepts MAJOR unset or 1, so pass the var only for
# the major run.
batch_ok=0
if [ "$MAJOR" = "1" ]; then
  make deps-batch MAJOR=1 && batch_ok=1 || true
else
  make deps-batch && batch_ok=1 || true
fi

manifests=(uv.lock tools/semgrep/uv.lock packages/vscode/package.json packages/vscode/package-lock.json)

if [ "$batch_ok" != "1" ]; then
  cat >&2 <<EOF

==> deps-batch verification FAILED (see output above).

The worktree is intact at:
  $wt

Fix forward — adapt our code to the new versions. Do NOT pin or revert (see the
dependency-upgrades skill). Then, from that worktree:
  make deps-batch MAJOR=$MAJOR                 # re-run upgrade + verify to green
  git add -A && git commit --signoff -m "Batch dependency upgrade $BRANCH (manual merge)"
  git push -u origin $BRANCH && gh pr create --base main --fill

If a fix genuinely cannot land this week, SKIP the batch and fix next week —
never pin to buy time. Security updates never defer.
EOF
  exit 1
fi

echo ""
echo "==> Upgraded manifests:"
git --no-pager diff --stat -- "${manifests[@]}"
echo ""

if [ "$DO_PR" != "1" ]; then
  cat <<EOF
==> Verified green. Review the diff above (npm-check-updates can overshoot under
    --major), then from:
    $wt

  git add -A && git commit --signoff -m "Batch dependency upgrade $BRANCH (manual merge)"
  git push -u origin $BRANCH && gh pr create --base main --fill

Dependabot closes its own PRs once these versions are on main. Merge is manual.
EOF
  exit 0
fi

echo "==> Committing, pushing, and opening the PR..."
major_note=""
[ "$MAJOR" = "1" ] && major_note=" MAJOR=1"
git add -A
git commit --signoff -m "Batch dependency upgrade $BRANCH (manual merge)"
git push -u origin "$BRANCH"
gh pr create --base main \
  --title "Batch dependency upgrade $BRANCH (manual merge)" \
  --body "Weekly dependency batch via \`make deps-batch${major_note}\`; \`validate-pr-raw\` green locally.

Do NOT auto-merge — a human merges. Dependabot closes its own PRs once these versions land on main."
echo "==> PR opened. Review and merge manually."
