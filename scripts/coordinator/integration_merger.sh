#!/usr/bin/env bash
# Coordinator loop: merge ready porchpin PRs into `integration`.
# A PR is ready when it targets integration and is code-reviewed, CI-green and
# conflict-free (mergeStateStatus CLEAN). PRs whose linked issue carries a maintainer
# ruling ("Maintainer ruling" at the top of the body) are SKIPPED and reported:
# the coordinator checks those by hand (#379 showed reviewers miss rulings; io #8141).
set -uo pipefail
COORD_DIR="${COORD_DIR:-$HOME/dev/worktree/issue-orchestrator/.coord}"  # runtime: locks, logs, exam output (not in git)
C="$COORD_DIR"
R=porchpin/porchpin
LOG=$C/integration_merger.log
END=$((SECONDS + ${HOURS:-12}*3600))
while [ $SECONDS -lt $END ]; do
  for row in $(gh pr list -R $R --base integration --state open --json number,headRefOid,mergeStateStatus,labels \
      -q '.[]|select(.mergeStateStatus=="CLEAN")|select([.labels[].name]|index("code-reviewed"))|"\(.number):\(.headRefOid[:8])"' 2>/dev/null); do
    pr=${row%%:*}
    issue=$(gh pr view $pr -R $R --json closingIssuesReferences,headRefName -q '(.closingIssuesReferences[0].number // (.headRefName|capture("^(?<n>[0-9]+)-").n))' 2>/dev/null)
    if [ -n "$issue" ] && gh issue view "$issue" -R $R --json body -q '.body' 2>/dev/null | grep -qE "io:standing-rulings:begin|Maintainer ruling"; then
      grep -q "^SKIP-RULING #$pr " $LOG 2>/dev/null || echo "SKIP-RULING #$pr issue #$issue $(date +%T): check against the ruling by hand" | tee -a $LOG
      continue
    fi
    echo "MERGING #$pr ($row) $(date +%T)" | tee -a $LOG
    if "$(dirname "$0")"/merge_into_integration.sh "$row" >> $LOG 2>&1; then echo "MERGED #$pr $(date +%T)" | tee -a $LOG
    else echo "FAILED #$pr $(date +%T) (see log)" | tee -a $LOG; fi
  done
  sleep 300
done
echo "merger loop ended $(date +%T)" | tee -a $LOG
