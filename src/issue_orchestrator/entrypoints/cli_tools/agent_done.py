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
from typing import NoReturn, Optional

import os

from ...domain.models import (
    CompletionRecord,
    CompletionOutcome,
    RequestedAction,
    COMPLETION_RECORD_PATH,
)
from ...control.validation import AgentGate, AgentGateResult


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


def extract_pr_verification_status(pr_body: str) -> tuple[bool, str | None]:
    """Extract verification status from PR body.

    Looks for the agent-done verification marker that proves the PR
    was created through the proper agent-done workflow.

    Args:
        pr_body: The PR description body text

    Returns:
        Tuple of (has_marker, token) where:
        - has_marker: True if verification marker is present
        - token: The verification token if present, None otherwise
    """
    import re

    # Look for verification marker pattern: <!-- agent-done-verified: <token> -->
    pattern = r'<!--\s*agent-done-verified:\s*([a-zA-Z0-9-]+)\s*-->'
    match = re.search(pattern, pr_body)

    if match:
        return (True, match.group(1))
    return (False, None)


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
        # PR labels
        pr_labels=getattr(args, 'pr_labels', None),
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

    # Orchestrator tells agent where to write via env var
    # This ensures each session type writes to a distinct file
    base_path = os.environ.get("ORCHESTRATOR_COMPLETION_PATH", COMPLETION_RECORD_PATH)
    output_path = worktree_root / base_path

    # If file exists (e.g., second review after rework), add numeric suffix
    if output_path.exists():
        stem = output_path.stem  # e.g., "completion-agent_review"
        suffix = output_path.suffix  # e.g., ".json"
        parent = output_path.parent
        counter = 2
        while output_path.exists():
            output_path = parent / f"{stem}-{counter}{suffix}"
            counter += 1

    with open(output_path, "w") as f:
        json.dump(record.to_dict(), f, indent=2)

    # Emit event for debugging - shows where completion was written
    from ...infra.emit import emit_event
    emit_event("completion.written", {
        "outcome": record.outcome.value,
        "path": str(output_path.resolve()),
        "env_path": base_path,
    })

    return output_path


def write_marker_file(status: str) -> None:
    """Write marker file indicating agent-done was called.

    This is checked by the Stop hook to detect sessions that exit without agent-done.
    """
    marker_file = Path(".agent-done-marker")
    marker_file.write_text(
        f"agent-done {status} called at {datetime.now().isoformat()}\n"
    )


def find_worktree_root() -> Path:
    """Find the worktree root by looking for .git."""
    cwd = Path.cwd()
    for path in [cwd, *cwd.parents]:
        if (path / ".git").exists():
            return path
    return cwd


def load_agent_gate_config(worktree: Path) -> tuple[Optional[str], int]:
    """Load agent gate configuration from the worktree.

    Uses the shared config lookup to find configuration and extract agent_gate config.

    Environment variable overrides (for testing):
    - ORCHESTRATOR_AGENT_GATE_CMD: Override the validation command
    - ORCHESTRATOR_AGENT_GATE_TIMEOUT: Override the timeout in seconds

    Args:
        worktree: Path to the worktree root

    Returns:
        Tuple of (command, timeout_seconds) or (None, 0) if not configured
    """
    import os
    from ...infra.config import load_validation_config

    # Check for environment variable override (useful for e2e tests)
    env_cmd = os.environ.get("ORCHESTRATOR_AGENT_GATE_CMD")
    env_timeout = os.environ.get("ORCHESTRATOR_AGENT_GATE_TIMEOUT")
    if env_cmd:
        timeout = int(env_timeout) if env_timeout else 120
        return env_cmd, timeout

    # Use shared config lookup (checks .issue-orchestrator/config/)
    validation_config = load_validation_config(worktree)

    agent_gate = validation_config["agent_gate"]
    policy = validation_config["policy"]

    # Only return command if policy enables it
    if policy["agent_runs"] == "agent_gate" and agent_gate["cmd"]:
        return agent_gate["cmd"], agent_gate["timeout_seconds"]

    return None, 0


def run_agent_gate(worktree: Path, verbose: bool = False) -> Optional[AgentGateResult]:
    """Run the agent gate validation if configured.

    Args:
        worktree: Path to the worktree root
        verbose: Whether to print validation output

    Returns:
        AgentGateResult if validation was run, None if not configured
    """
    cmd, timeout = load_agent_gate_config(worktree)
    if not cmd:
        return None

    if verbose:
        print(f"Running agent gate validation: {cmd}")

    from ...execution import LocalCommandRunner, GitWorkingCopy

    gate = AgentGate(
        worktree,
        command_runner=LocalCommandRunner(),
        working_copy=GitWorkingCopy(),
        command=cmd,
        timeout_seconds=timeout,
    )
    result = gate.run()

    if verbose:
        if result.passed:
            print(f"✓ Agent gate passed: {result.reason}")
        else:
            print(f"✗ Agent gate failed: {result.reason}")

    return result


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

    # PR options
    parser.add_argument(
        "--pr-labels",
        nargs="+",
        help="Extra labels to add to the PR (e.g., --pr-labels test-data needs-review)",
    )

    # Meta options
    parser.add_argument("--dry-run", action="store_true", help="Show what would be written")
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip agent gate validation even if configured",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed output",
    )

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

    # Find worktree root
    worktree_root = find_worktree_root()

    # Run agent gate validation if configured (and not skipped)
    validation_result = None
    if not args.skip_validation:
        validation_result = run_agent_gate(worktree_root, verbose=args.verbose)
        if validation_result and not validation_result.passed:
            print(f"\n❌ Agent gate validation failed: {validation_result.reason}")
            print("Cannot complete agent work until validation passes.")
            if validation_result.record_path:
                print(f"Validation record: {validation_result.record_path}")
            if validation_result.record and validation_result.record.stdout_path:
                print(f"Stdout: {validation_result.record.stdout_path}")
            if validation_result.record and validation_result.record.stderr_path:
                print(f"Stderr: {validation_result.record.stderr_path}")
            print("Fix the issues and run agent-done again.")
            sys.exit(1)

    # If validation passed, include the record path in the completion record
    if validation_result and validation_result.passed and validation_result.record_path:
        record.validation_record_path = validation_result.record_path

    # Write marker file first (indicates agent-done was called)
    write_marker_file(status)

    # Write completion record
    output_path = write_completion_record(record)

    print(f"Completion record written to: {output_path}")
    print(f"Status: {status}")
    print(f"Session: {record.session_id}")
    if validation_result:
        validation_status = "passed" if validation_result.passed else "failed"
        print(f"Validation: {validation_status}")
    print("\nThe orchestrator will process this record and perform the necessary actions.")


if __name__ == "__main__":
    main()
