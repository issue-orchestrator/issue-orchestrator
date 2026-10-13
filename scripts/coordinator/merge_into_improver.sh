#!/usr/bin/env bash
# Merge an approved sub-PR into tech-lead-improver, gate the MERGE RESULT, push.
#   usage: merge_into_improver.sh <pr>:<approved-head-sha-prefix>
#   env:   IGNORE_CHECKS=1 skips waiting on the PR's CI (use when CI is known-broken)
# Runs in the tech-lead-improver worktree. One at a time: it holds the shared
# gate lock (.coord/gate.lock) for the whole merge+gate+push.
set -euo pipefail

COORD_DIR="${COORD_DIR:-$HOME/dev/worktree/issue-orchestrator/.coord}"  # runtime: locks, logs, exam output (not in git)
COORD="$COORD_DIR"
LOCK="$COORD/gate.lock"
WT=~/dev/worktree/issue-orchestrator/tech-lead-improver
REPO=issue-orchestrator/issue-orchestrator
BASE=tech-lead-improver

arg="${1:?usage: merge_into_improver.sh <pr>:<sha>}"
PR="${arg%%:*}"; WANT="${arg#*:}"
[ -n "$PR" ] && [ -n "$WANT" ] && [ "$PR" != "$WANT" ] || { echo "usage: <pr>:<sha>" >&2; exit 2; }

read -r HEAD_SHA HEAD_REF PR_BASE STATE < <(gh pr view "$PR" -R "$REPO" \
  --json headRefOid,headRefName,baseRefName,state \
  -q '"\(.headRefOid) \(.headRefName) \(.baseRefName) \(.state)"')
[ "$STATE" = "OPEN" ] || { echo "PR #$PR is $STATE, not OPEN" >&2; exit 1; }
[ "$PR_BASE" = "$BASE" ] || { echo "PR #$PR base is $PR_BASE, not $BASE" >&2; exit 1; }
case "$HEAD_SHA" in "$WANT"*) ;; *) echo "PR #$PR head ${HEAD_SHA:0:7} != approved $WANT; refusing" >&2; exit 1;; esac

if [ "${IGNORE_CHECKS:-0}" != "1" ]; then
  # gh pr checks is tab-separated: name<TAB>state<TAB>...; names contain spaces.
  # No checks listed yet is NOT green: CI may not have registered (2026-10-09:
  # validate-fast failed on every merged sub-PR unnoticed).
  # Wait for the suite check by NAME: validate-agent registers first and alone.
  until gh pr checks "$PR" -R "$REPO" 2>/dev/null | cut -f1 | grep -qx validate-fast; do sleep 60; done
  until ! gh pr checks "$PR" -R "$REPO" 2>/dev/null | cut -f2 | grep -qx pending; do sleep 60; done
  if gh pr checks "$PR" -R "$REPO" 2>/dev/null | cut -f2 | grep -qxE "fail|cancel"; then
    echo "PR #$PR has failing checks; refusing (IGNORE_CHECKS=1 to override)" >&2
    gh pr checks "$PR" -R "$REPO" | cut -f1,2 >&2; exit 1
  fi
fi

until mkdir "$LOCK" 2>/dev/null; do sleep 30; done
echo "pid=$$ merge #$PR $(date -Iseconds)" > "$LOCK/owner"
trap 'rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null || true' EXIT

cd "$WT"
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "worktree dirty; refusing" >&2; exit 1; }
git fetch -q origin "$BASE" "$HEAD_REF"
git checkout -q "$BASE"
git reset -q --hard "origin/$BASE"
[ "$(git rev-parse "origin/$HEAD_REF")" = "$HEAD_SHA" ] || { echo "fetched head differs from PR head; refusing" >&2; exit 1; }
TITLE="$(gh pr view "$PR" -R "$REPO" --json title -q .title)"
git merge -q --no-ff "$HEAD_SHA" -m "Merge #$PR: $TITLE" || {
  git merge --abort; echo "merge conflict: #$PR does not merge cleanly into $BASE; worktree restored" >&2; exit 1; }
NEW="$(git rev-parse --short HEAD)"
echo "merged #$PR (${HEAD_SHA:0:7}) -> $NEW"

set +e
make validate-pr
rc=$?
set -e
echo "gate rc=$rc at $NEW"
if [ $rc -ne 0 ]; then
  git reset -q --hard "origin/$BASE"
  echo "gate failed: merge NOT pushed; worktree reset to origin/$BASE" >&2
  exit 1
fi
git push origin "$BASE"
git ls-remote --heads origin "$BASE"
