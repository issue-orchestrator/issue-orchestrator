"""Agent completion CLI - writes structured completion record.

This command is the ONLY sanctioned way for agents to complete their work.
It writes a structured JSON completion record that the orchestrator processes.

Architecture principle: The agent reports intent; the orchestrator decides and executes.

The agent does NOT:
- Push code
- Create PRs
- Post comments
- Mutate labels

All those actions are performed by the orchestrator after validating
the completion record as untrusted input.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from .models import (
    CompletionRecord,
    CompletionOutcome,
    RequestedAction,
    COMPLETION_RECORD_PATH,
)


class AgentStatus:
    """Allowed completion statuses from CLI. Maps to CompletionOutcome."""
    COMPLETED = "completed"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


# Required fields per status - agents MUST provide these
REQUIRED_FIELDS = {
    AgentStatus.COMPLETED: ["implementation", "problems"],
    AgentStatus.BLOCKED: ["reason", "attempted"],
    AgentStatus.NEEDS_HUMAN: ["question"],
    AgentStatus.APPROVED: ["summary", "risk"],
    AgentStatus.CHANGES_REQUESTED: ["issues", "risk"],
}

# Map CLI status to CompletionOutcome
STATUS_TO_OUTCOME = {
    AgentStatus.COMPLETED: CompletionOutcome.COMPLETED,
    AgentStatus.BLOCKED: CompletionOutcome.BLOCKED,
    AgentStatus.NEEDS_HUMAN: CompletionOutcome.NEEDS_HUMAN,
    AgentStatus.APPROVED: CompletionOutcome.REVIEW_APPROVED,
    AgentStatus.CHANGES_REQUESTED: CompletionOutcome.REVIEW_CHANGES_REQUESTED,
}

# Map CLI status to requested actions
STATUS_TO_ACTIONS = {
    AgentStatus.COMPLETED: [
        RequestedAction.PUSH_BRANCH,
        RequestedAction.CREATE_PR,
        RequestedAction.POST_COMMENT,
    ],
    AgentStatus.BLOCKED: [
        RequestedAction.PUSH_BRANCH,
        RequestedAction.ADD_BLOCKED_LABEL,
        RequestedAction.POST_COMMENT,
    ],
    AgentStatus.NEEDS_HUMAN: [
        RequestedAction.PUSH_BRANCH,
        RequestedAction.ADD_NEEDS_HUMAN_LABEL,
        RequestedAction.POST_COMMENT,
    ],
    AgentStatus.APPROVED: [
        RequestedAction.ADD_CODE_REVIEWED_LABEL,
        RequestedAction.REMOVE_CODE_REVIEW_LABEL,
        RequestedAction.POST_COMMENT,
    ],
    AgentStatus.CHANGES_REQUESTED: [
        RequestedAction.ADD_NEEDS_REWORK_LABEL,
        RequestedAction.REMOVE_CODE_REVIEW_LABEL,
        RequestedAction.POST_COMMENT,
    ],
}


def die(message: str) -> NoReturn:
    """Print error and exit with failure."""
    print(f"ERROR: {message}", file=sys.stderr)
    print("\nUse --help for usage information.", file=sys.stderr)
    sys.exit(1)


def get_session_id() -> str:
    """Get session ID from environment or generate one.

    The orchestrator sets ORCHESTRATOR_SESSION_ID when launching agents.
    If not set, we generate a timestamp-based ID for standalone usage.
    """
    import os
    session_id = os.environ.get("ORCHESTRATOR_SESSION_ID")
    if session_id:
        return session_id
    # Fallback for standalone usage
    return f"standalone-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def validate_fields(status: str, args: argparse.Namespace) -> None:
    """Validate all required fields are present for the status."""
    required = REQUIRED_FIELDS[status]
    missing = []

    for field in required:
        value = getattr(args, field, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(f"--{field.replace('_', '-')}")

    if missing:
        die(f"Status '{status}' requires: {', '.join(missing)}")


def format_comment_body(status: str, args: argparse.Namespace) -> str:
    """Format the comment body based on status and args."""
    if status == AgentStatus.COMPLETED:
        return f"""## Implementation

{args.implementation}

## Problems Encountered

{args.problems}"""

    elif status == AgentStatus.BLOCKED:
        blocked_by = ""
        if args.blocked_by:
            refs = ", ".join(f"#{n}" for n in args.blocked_by)
            blocked_by = f"\n**Blocked by:** {refs}"

        when_unblocked = ""
        if args.when_unblocked:
            when_unblocked = f"\n\n**When unblocked:** {args.when_unblocked}"

        return f"""## Blocked

**Reason:** {args.reason}{blocked_by}
**Attempted:** {args.attempted}{when_unblocked}"""

    elif status == AgentStatus.NEEDS_HUMAN:
        parts = [f"## Needs Human Input\n\n**Question:** {args.question}"]
        if args.context:
            parts.append(f"**Context:** {args.context}")
        if args.options:
            parts.append("**Options:**")
            for i, opt in enumerate(args.options, 1):
                parts.append(f"{i}. {opt}")
        if args.default:
            parts.append(f"**Default if no response:** {args.default}")
        return "\n".join(parts)

    elif status == AgentStatus.APPROVED:
        risk_emoji = {"low": "G", "medium": "Y", "high": "R"}[args.risk]
        checks_str = ", ".join(f"`{c}`" for c in (args.checks or []))
        return f"""## Code Review Approved

{args.summary}

---
<!-- VERDICT_START -->
**Verdict:** `approve`
**Risk:** {risk_emoji} `{args.risk}`
{f"**Checks passed:** {checks_str}" if checks_str else ""}
<!-- VERDICT_END -->"""

    else:  # CHANGES_REQUESTED
        risk_emoji = {"low": "G", "medium": "Y", "high": "R"}[args.risk]
        checks_str = ", ".join(f"`{c}`" for c in (args.checks_needed or []))
        return f"""## Changes Requested

{args.issues}

---
<!-- VERDICT_START -->
**Verdict:** `request_changes`
**Risk:** {risk_emoji} `{args.risk}`
{f"**Checks needed:** {checks_str}" if checks_str else ""}
<!-- VERDICT_END -->

*The work agent will be re-queued to address these issues.*"""


def build_completion_record(status: str, args: argparse.Namespace) -> CompletionRecord:
    """Build a CompletionRecord from CLI arguments."""
    outcome = STATUS_TO_OUTCOME[status]
    actions = STATUS_TO_ACTIONS[status]

    # Format summary
    if status == AgentStatus.COMPLETED:
        summary = f"Completed: {args.implementation[:100]}..."
    elif status == AgentStatus.BLOCKED:
        summary = f"Blocked: {args.reason[:100]}..."
    elif status == AgentStatus.NEEDS_HUMAN:
        summary = f"Needs human: {args.question[:100]}..."
    elif status == AgentStatus.APPROVED:
        summary = f"Approved: {args.summary[:100]}..."
    else:
        summary = f"Changes requested: {args.issues[:100]}..."

    # Build comment body
    comment_body = format_comment_body(status, args)

    return CompletionRecord(
        session_id=get_session_id(),
        timestamp=datetime.now().isoformat(),
        outcome=outcome,
        summary=summary,
        requested_actions=actions,
        # Completion fields
        implementation=args.implementation if status == AgentStatus.COMPLETED else None,
        problems=args.problems if status == AgentStatus.COMPLETED else None,
        # Blocked fields
        blocked_reason=args.reason if status == AgentStatus.BLOCKED else None,
        blocked_by=args.blocked_by if status == AgentStatus.BLOCKED else None,
        attempted=args.attempted if status == AgentStatus.BLOCKED else None,
        when_unblocked=args.when_unblocked if status == AgentStatus.BLOCKED else None,
        # Needs human fields
        question=args.question if status == AgentStatus.NEEDS_HUMAN else None,
        context=args.context if status == AgentStatus.NEEDS_HUMAN else None,
        options=args.options if status == AgentStatus.NEEDS_HUMAN else None,
        default_action=args.default if status == AgentStatus.NEEDS_HUMAN else None,
        # Review fields
        review_summary=args.summary if status == AgentStatus.APPROVED else None,
        review_issues=args.issues if status == AgentStatus.CHANGES_REQUESTED else None,
        risk_level=args.risk if status in (AgentStatus.APPROVED, AgentStatus.CHANGES_REQUESTED) else None,
        checks_passed=args.checks if status == AgentStatus.APPROVED else None,
        checks_needed=args.checks_needed if status == AgentStatus.CHANGES_REQUESTED else None,
        # Comment to post
        comment_body=comment_body,
    )


def write_completion_record(record: CompletionRecord) -> Path:
    """Write the completion record to JSON file.

    Returns the path to the written file.
    """
    # Find worktree root (handles being in subdirectory)
    cwd = Path.cwd()
    worktree_root = cwd

    # Look for .git file/directory to find root
    for path in [cwd, *cwd.parents]:
        if (path / ".git").exists():
            worktree_root = path
            break

    # Create .issue-orchestrator directory if needed
    output_dir = worktree_root / ".issue-orchestrator"
    output_dir.mkdir(exist_ok=True)

    # Write completion record
    output_path = worktree_root / COMPLETION_RECORD_PATH
    with open(output_path, "w") as f:
        json.dump(record.to_dict(), f, indent=2)

    return output_path


def write_marker_file(status: str) -> None:
    """Write marker file indicating agent-done was called.

    This is checked by the Stop hook to detect sessions that exit without agent-done.
    """
    marker_file = Path(".agent-done-marker")
    marker_file.write_text(
        f"agent-done {status} called at {datetime.now().isoformat()}\n"
    )


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Complete agent work with structured status reporting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
  Completed successfully:
    agent-done completed --implementation "Added user auth" --problems "None"

  Blocked:
    agent-done blocked --reason "Need API credentials" --attempted "Checked env vars"

  Need human input:
    agent-done needs_human --question "Should we use OAuth or API keys?"

  Review approved:
    agent-done approved --summary "Code is clean" --risk low --checks tests_added

  Review requests changes:
    agent-done changes_requested --issues "Missing error handling" --risk medium

STATUSES:
  completed          - Work done, PR ready (requires: --implementation, --problems)
  blocked            - Cannot proceed (requires: --reason, --attempted)
  needs_human        - Need decision (requires: --question)
  approved           - Review passed (requires: --summary, --risk)
  changes_requested  - Review needs fixes (requires: --issues, --risk)

This command writes a completion record to .issue-orchestrator/completion.json.
The orchestrator reads this file and performs the necessary actions (push, PR, labels).
"""
    )

    # Positional: status (required)
    parser.add_argument(
        "status",
        choices=["completed", "blocked", "needs_human", "approved", "changes_requested"],
        help="Completion status"
    )

    # Completion fields
    parser.add_argument("--implementation", "-i", help="What was implemented")
    parser.add_argument("--problems", "-p", help="Problems encountered")

    # Blocked fields
    parser.add_argument("--reason", "-r", help="Why blocked")
    parser.add_argument("--attempted", "-a", help="What was attempted")
    parser.add_argument("--blocked-by", "-b", type=int, nargs="+", help="Blocking issue numbers")
    parser.add_argument("--when-unblocked", "-w", help="Hint for when blocker is resolved")

    # Needs human fields
    parser.add_argument("--question", "-q", help="Question for human")
    parser.add_argument("--context", "-c", help="Context for the question")
    parser.add_argument("--options", "-o", nargs="+", help="Available options")
    parser.add_argument("--default", help="Default action if no response")

    # Reviewer fields
    parser.add_argument("--summary", "-s", help="Summary of review")
    parser.add_argument("--issues", help="Issues found that need fixing")
    parser.add_argument("--risk", choices=["low", "medium", "high"], help="Risk level")
    parser.add_argument("--checks", nargs="+", help="Checks that passed")
    parser.add_argument("--checks-needed", nargs="+", help="Checks that need to be done")

    # Meta options
    parser.add_argument("--dry-run", action="store_true", help="Show what would be written")

    args = parser.parse_args()
    status = args.status

    # Validate required fields
    validate_fields(status, args)

    # Build completion record
    record = build_completion_record(status, args)

    if args.dry_run:
        print("--- DRY RUN: Would write this completion record ---")
        print(json.dumps(record.to_dict(), indent=2))
        print("--- END ---")
        return

    # Write marker file first (indicates agent-done was called)
    write_marker_file(status)

    # Write completion record
    output_path = write_completion_record(record)

    print(f"Completion record written to: {output_path}")
    print(f"Status: {status}")
    print(f"Session: {record.session_id}")
    print("\nThe orchestrator will process this record and perform the necessary actions.")


if __name__ == "__main__":
    main()
