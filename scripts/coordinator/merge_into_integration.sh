#!/usr/bin/env bash
# Merge an approved porchpin PR into the `integration` branch (operator-sanctioned
# process, 2026-10-04: "go ahead with this branch idea"). The push runs porchpin's
# pre-push gate on the MERGED result, so integration only moves on a green merge.
#   usage: merge_into_integration.sh <pr>:<approved-head-sha-prefix>
set -euo pipefail
REPO=porchpin/porchpin
BASE=integration
WT=~/dev/worktree/porchpin/integration-merge
arg="${1:?usage: <pr>:<sha>}"; PR="${arg%%:*}"; WANT="${arg#*:}"

read -r HEAD_SHA HEAD_REF PR_BASE STATE < <(gh pr view "$PR" -R "$REPO" \
  --json headRefOid,headRefName,baseRefName,state -q '"\(.headRefOid) \(.headRefName) \(.baseRefName) \(.state)"')
[ "$STATE" = OPEN ] || { echo "PR #$PR is $STATE" >&2; exit 1; }
[ "$PR_BASE" = "$BASE" ] || { echo "PR #$PR base is $PR_BASE, not $BASE" >&2; exit 1; }
case "$HEAD_SHA" in "$WANT"*) ;; *) echo "PR #$PR head ${HEAD_SHA:0:8} != approved $WANT" >&2; exit 1;; esac
gh pr view "$PR" -R "$REPO" --json labels -q '[.labels[].name]|index("code-reviewed")' | grep -qv null \
  || { echo "PR #$PR is not code-reviewed" >&2; exit 1; }
until ! gh pr checks "$PR" -R "$REPO" 2>/dev/null | cut -f2 | grep -qx pending; do sleep 30; done
if gh pr checks "$PR" -R "$REPO" 2>/dev/null | cut -f2 | grep -qxE "fail|cancel"; then
  echo "PR #$PR has failing checks" >&2; gh pr checks "$PR" -R "$REPO" | cut -f1,2 >&2; exit 1
fi

if [ ! -d "$WT" ]; then (cd ~/dev/porchpin && git worktree add -q --detach "$WT" origin/$BASE); fi
cd "$WT"
git fetch -q origin "$BASE" "$HEAD_REF"
git checkout -q --detach "origin/$BASE"
[ "$(git rev-parse "origin/$HEAD_REF")" = "$HEAD_SHA" ] || { echo "fetched head differs" >&2; exit 1; }
TITLE="$(gh pr view "$PR" -R "$REPO" --json title -q .title)"
git merge -q --no-ff "$HEAD_SHA" -m "Merge #$PR: $TITLE" -m "Merged into $BASE by the coordinator (approved: code-reviewed, CI green)." \
  || { git merge --abort; echo "PR #$PR conflicts with $BASE; needs engine rework" >&2; exit 1; }
NEW="$(git rev-parse --short HEAD)"
echo "merged #$PR (${HEAD_SHA:0:8}) -> $NEW on $BASE"
[ -d node_modules ] || npm ci --silent >/dev/null 2>&1
git push origin "HEAD:refs/heads/$BASE"
git ls-remote --heads origin "$BASE"
