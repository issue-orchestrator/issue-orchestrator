#!/usr/bin/env bash
# Shared helper for simulated scenario scripts.
# Writes agent intent, then submits coding intent through the real authenticated
# intake producer. The configured orchestrator validator owns acceptance.
#
# Usage:
#   source "$(dirname "$0")/_write_completion.sh"
#   write_completion completed "Simulated scenario completed" "None"
#   write_completion blocked "Cannot proceed" "Tried X"
#   write_completion approved "Looks good" "low"
#   write_completion changes_requested "Needs fixes" "medium"
#   write_completion needs_human "Need decision"

write_completion() {
  local outcome="$1"
  local field1="${2:-}"
  local field2="${3:-}"
  if [[ -n "${SCENARIO_COMPLETION_ATTEMPT:-}" ]]; then
    field1="${field1} (attempt ${SCENARIO_COMPLETION_ATTEMPT})"
  fi

  local completion_path="${ISSUE_ORCHESTRATOR_COMPLETION_PATH:-}"
  if [[ -z "$completion_path" ]]; then
    completion_path=".issue-orchestrator/completion.json"
  fi

  # Resolve relative path
  if [[ "$completion_path" != /* ]]; then
    completion_path="$(pwd)/$completion_path"
  fi
  mkdir -p "$(dirname "$completion_path")"

  local session_id="${ISSUE_ORCHESTRATOR_SESSION_ID:-sim-session}"
  local timestamp
  timestamp=$(date -u +"%Y-%m-%dT%H:%M:%S")

  # Build JSON based on outcome
  case "$outcome" in
    completed)
      cat > "$completion_path" <<ENDJSON
{
  "session_id": "${session_id}",
  "timestamp": "${timestamp}",
  "outcome": "completed",
  "summary": "Completed: ${field1}",
  "requested_actions": ["push_branch", "create_pr", "post_comment"],
  "implementation": "${field1}",
  "problems": "${field2}",
  "comment_body": "## Implementation\n${field1}\n## Problems\n${field2}"
}
ENDJSON
      ;;
    blocked)
      cat > "$completion_path" <<ENDJSON
{
  "session_id": "${session_id}",
  "timestamp": "${timestamp}",
  "outcome": "blocked",
  "summary": "Blocked: ${field1}",
  "requested_actions": ["push_branch", "add_blocked_label", "post_comment"],
  "blocked_reason": "${field1}",
  "attempted": "${field2}",
  "comment_body": "## Blocked\n**Reason:** ${field1}\n**Attempted:** ${field2}"
}
ENDJSON
      ;;
    approved)
      cat > "$completion_path" <<ENDJSON
{
  "session_id": "${session_id}",
  "timestamp": "${timestamp}",
  "outcome": "review_approved",
  "summary": "Approved: ${field1}",
  "requested_actions": ["add_code_reviewed_label", "remove_needs_rework_label", "remove_code_review_label", "post_comment"],
  "review_summary": "${field1}",
  "risk_level": "${field2:-low}",
  "comment_body": "## Code Review Approved\n${field1}"
}
ENDJSON
      ;;
    changes_requested)
      cat > "$completion_path" <<ENDJSON
{
  "session_id": "${session_id}",
  "timestamp": "${timestamp}",
  "outcome": "review_changes_requested",
  "summary": "Changes requested: ${field1}",
  "requested_actions": ["add_needs_rework_label", "remove_code_review_label", "post_comment"],
  "review_issues": "${field1}",
  "risk_level": "${field2:-medium}",
  "comment_body": "## Changes Requested\n${field1}"
}
ENDJSON
      ;;
    needs_human)
      cat > "$completion_path" <<ENDJSON
{
  "session_id": "${session_id}",
  "timestamp": "${timestamp}",
  "outcome": "needs_human",
  "summary": "Needs human: ${field1}",
  "requested_actions": ["push_branch", "add_needs_human_label", "post_comment"],
  "question": "${field1}",
  "comment_body": "## Needs Human Input\n**Question:** ${field1}"
}
ENDJSON
      ;;
    *)
      echo "Unknown outcome: $outcome" >&2
      return 1
      ;;
  esac

  case "$outcome" in
    completed|blocked|needs_human)
      python - "$completion_path" <<'PYTHON'
import sys
from pathlib import Path
from issue_orchestrator.entrypoints.cli_tools.completion_submit import submit_completion_file
receipt = submit_completion_file(Path(sys.argv[1]))
print(f"Completion receipt received: {receipt.entry_id}")
PYTHON
      ;;
  esac
  echo "Completion record written: outcome=${outcome} path=${completion_path}"
}
