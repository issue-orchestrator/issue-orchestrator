#!/usr/bin/env bash
# Redeploy the Control Center + porchpin engine onto the tech-lead-improver worktree's
# current HEAD, at the first idle moment (active=0), so no agent session is killed.
#   usage: deploy_porchpin.sh <expected-short-sha> [reason]
set -uo pipefail
COORD_DIR="${COORD_DIR:-$HOME/dev/worktree/issue-orchestrator/.coord}"  # runtime: locks, logs, exam output (not in git)
C="$COORD_DIR"
WT=$HOME/dev/worktree/issue-orchestrator/tech-lead-improver
L=$HOME/dev/porchpin/.issue-orchestrator/state/logs/orchestrator.log
WANT="${1:?usage: deploy_porchpin.sh <sha> [reason]}"; REASON="${2:-Coordinator deploy of tech-lead-improver $WANT}"
HEAD=$(git -C "$WT" rev-parse --short HEAD)
case "$HEAD" in "$WANT"*) ;; *) echo "worktree at $HEAD, not $WANT"; exit 1;; esac
end=$((SECONDS+21600))
while [ $SECONDS -lt $end ]; do
  a=$(grep "\[LOOP\] Iteration" "$L" | tail -1 | grep -oE "active=[0-9]+" | cut -d= -f2)
  [ "$a" = "0" ] && break; sleep 60
done
[ "$a" = "0" ] || { echo "never idle in 6h"; exit 1; }
echo "idle at $(date +%T)"
# The worktree can move while we wait for idle (a merge into tech-lead-improver);
# deploying then would ship an unexamined commit. Re-check right before acting.
HEAD=$(git -C "$WT" rev-parse --short HEAD)
case "$HEAD" in "$WANT"*) ;; *) echo "worktree moved to $HEAD while waiting; NOT deploying $WANT"; exit 1;; esac
T=$(cat ~/.issue-orchestrator/api-token)
curl -s -m 60 -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d "{\"repo_root\":\"$HOME/dev/porchpin\",\"reason\":\"$REASON\"}" \
  http://127.0.0.1:19080/control/orchestrator/stop >/dev/null
for i in $(seq 30); do lsof -iTCP:8081 -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 2; done
CCPID=$(lsof -tiTCP:19080 -sTCP:LISTEN | head -1)
[ -n "$CCPID" ] && { echo "stopping CC pid $CCPID"; kill "$CCPID"; }
for i in $(seq 40); do lsof -iTCP:19080 -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 3; done
# Start the CC in its OWN session (it must outlive the coordinator's Claude Code
# process: on 2026-10-13 a hung session took the CC and porchpin down with it) and
# with no CLAUDE* identity or PYTHONPATH from this shell (#9293: agents would lose
# their transcripts).
python3 - "$WT/scripts" "$C/cc_start_$WANT.log" <<'PY'
import os, subprocess, sys
env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE") and k != "PYTHONPATH"}
subprocess.Popen(["./start_control_center.sh"], cwd=sys.argv[1], env=env, stdin=subprocess.DEVNULL,
                 stdout=open(sys.argv[2], "ab"), stderr=subprocess.STDOUT, start_new_session=True)
PY
for i in $(seq 60); do lsof -iTCP:19080 -sTCP:LISTEN >/dev/null 2>&1 && break; sleep 3; done; sleep 5
curl -s -m 180 -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d "{\"repo_root\":\"$HOME/dev/porchpin\",\"mode\":\"default\",\"config_name\":\"main.yaml\"}" \
  http://127.0.0.1:19080/control/orchestrator/start \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("status"), d.get("doctor",{}).get("overall"), d.get("error")); [print("  doctor:",c["name"],c["status"],c["detail"][:110]) for c in d.get("doctor",{}).get("checks",[]) if c["status"] not in ("ok","info")]'
unset T
for i in $(seq 30); do grep -q "Recorded engine start.*engine commit $WANT" "$L" && break; sleep 3; done
grep "Recorded engine start" "$L" | tail -1 | cut -c1-200
