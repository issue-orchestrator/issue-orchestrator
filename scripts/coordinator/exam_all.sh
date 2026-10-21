#!/usr/bin/env bash
# Run every tech-lead exam case against one engine ref, under .coord/exam.lock.
#   usage: exam_all.sh <sha>   -> logs .coord/exam/<CASE>-<sha>.log, summary on stdout
set -uo pipefail
COORD_DIR="${COORD_DIR:-$HOME/dev/worktree/issue-orchestrator/.coord}"  # runtime: locks, logs, exam output (not in git)
C="$COORD_DIR"; REF="${1:?sha}"
LOCK=$C/exam.lock
until mkdir "$LOCK" 2>/dev/null; do sleep 30; done
echo "pid=$$ exam $REF $(date -Iseconds)" > "$LOCK/owner"
trap 'rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null || true' EXIT
cd $HOME/dev/worktree/issue-orchestrator/exam-runner
for c in A B C D E F G H I J U; do
  log=$C/exam/$c-$REF.log
  E2E_TECH_LEAD_EXAM=1 E2E_EXAM_OUT=$HOME/dev/issue-orchestrator/.git/io-tech-lead-exam E2E_EXAM_ENGINE_REF=$REF \
    .venv/bin/pytest tests/e2e/test_tech_lead_exam.py -m tech_lead_exam -v -s --tb=short -k "case-$c-" --durations=10 --durations-min=1.0 > "$log" 2>&1
  echo "$c rc=$? $(grep -E '^=+ .*(passed|failed|error).* in ' "$log" | tail -1)"
done
