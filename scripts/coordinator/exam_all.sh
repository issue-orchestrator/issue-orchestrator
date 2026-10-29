#!/usr/bin/env bash
# Run every tech-lead exam case against one engine ref, under .coord/exam.lock.
#   usage: exam_all.sh <sha>   -> logs .coord/exam/<CASE>-<sha>.log, summary on stdout
# The cases are whatever pytest collects from tests/e2e/test_tech_lead_exam.py
# (.coord/exam/collect-<sha>.log); no case collected is a failure, not a pass.
set -uo pipefail
COORD_DIR="${COORD_DIR:-$HOME/dev/worktree/issue-orchestrator/.coord}"  # runtime: locks, logs, exam output (not in git)
C="$COORD_DIR"; REF="${1:?sha}"
# The live suite runs only on IO_E2E=1 and only against a dedicated test
# repository, never issue-orchestrator/issue-orchestrator (#9523). The exam's is
# io-e2e-sandbox: a private copy of io main with validate CI enabled, because
# case B needs green checks. Only an unset E2E_TEST_REPO takes that default; an
# empty one is refused here, before taking the lock, rather than once per case.
export IO_E2E=1
export E2E_TEST_REPO="${E2E_TEST_REPO-issue-orchestrator/io-e2e-sandbox}"
if [[ -z "${E2E_TEST_REPO//[[:space:]]/}" ]]; then
  echo "exam_all.sh: E2E_TEST_REPO is empty; name a dedicated test repository for the exam, or unset it for issue-orchestrator/io-e2e-sandbox (#9523)" >&2
  exit 2
fi
LOCK=$C/exam.lock
until mkdir "$LOCK" 2>/dev/null; do sleep 30; done
echo "pid=$$ exam $REF $(date -Iseconds)" > "$LOCK/owner"
trap 'rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null || true' EXIT
cd $HOME/dev/worktree/issue-orchestrator/exam-runner
export E2E_TECH_LEAD_EXAM=1 E2E_EXAM_OUT=$HOME/dev/issue-orchestrator/.git/io-tech-lead-exam E2E_EXAM_ENGINE_REF=$REF
# The cases are the ones pytest collects from the exam, never a list kept here:
# a hard-coded loop silently skipped case K (#9542). Collected under the lock,
# from the harness that then runs them.
collected=$C/exam/collect-$REF.log
.venv/bin/pytest tests/e2e/test_tech_lead_exam.py -m tech_lead_exam --collect-only -q > "$collected" 2>&1
collect_rc=$?
# Only a case's node id: live log lines and the summary never match, and the
# warnings summary may name a case a second time.
nodes=$(grep -E '^[^ ]+\.py::test_tech_lead_exam\[case-[^] ]+\]$' "$collected" | awk '!seen[$0]++')
if (( collect_rc != 0 )) || [[ -z "$nodes" ]]; then
  echo "exam_all.sh: collecting the exam found no case to run (pytest rc=$collect_rc); refusing to report an empty exam as a full one (#9542). Collection output: $collected" >&2
  tail -n 20 "$collected" >&2
  exit 3
fi
# fd 3, so a case's pytest cannot read the rest of the list from stdin.
while IFS= read -r node <&3; do
  id=${node##*\[case-}; c=${id%%-*}
  log=$C/exam/$c-$REF.log
  .venv/bin/pytest "$node" -m tech_lead_exam -v -s --tb=short --durations=10 --durations-min=1.0 > "$log" 2>&1
  echo "$c rc=$? $(grep -E '^=+ .*(passed|failed|error).* in ' "$log" | tail -1)"
done 3<<< "$nodes"
