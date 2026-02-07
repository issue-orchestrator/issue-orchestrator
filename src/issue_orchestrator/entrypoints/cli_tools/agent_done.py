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
import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import NoReturn, Optional

import os

from ...infra.logging_config import issue_log
from ...infra.env import get_env

logger = logging.getLogger(__name__)

from ...domain.models import (
    CompletionRecord,
    CompletionOutcome,
    RequestedAction,
    COMPLETION_RECORD_PATH,
)
from ...control.validation import AgentGate, AgentGateResult
from ...execution.session_output_adapter import FileSystemSessionOutput


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


def _record_validation_artifacts(  # noqa: C901 - handles multiple artifact types (manifest, stdout, stderr) with conditional paths
    worktree_root: Path,
    session_id: str | None,
    validation_result: AgentGateResult,
) -> None:
    """Attach validation artifacts to the session output for diagnostics."""
    if not session_id:
        return
    record = validation_result.record
    record_path = validation_result.record_path
    if not record_path and not record:
        return

    session_output = FileSystemSessionOutput()
    run_dir = session_output.ensure_run_dir(worktree_root, session_id)
    updates: dict[str, str] = {}
    if record_path:
        updates["validation_record_path"] = record_path
    updates["validation_status"] = "passed" if validation_result.passed else "failed"
    if not validation_result.passed:
        updates["validation_reason"] = validation_result.reason

    if updates:
        session_output.update_manifest(run_dir, updates)

    if not record:
        return

    if record.stdout_path:
        stdout_src = worktree_root / record.stdout_path
        if stdout_src.exists():
            stdout_dest = run_dir / "validation-stdout.log"
            try:
                stdout_dest.write_text(stdout_src.read_text(errors="ignore"))
                session_output.update_manifest(run_dir, {"validation_stdout": str(stdout_dest)})
            except OSError:
                logger.debug("Failed to write validation stdout for %s", run_dir)
    if record.stderr_path:
        stderr_src = worktree_root / record.stderr_path
        if stderr_src.exists():
            stderr_dest = run_dir / "validation-stderr.log"
            try:
                stderr_dest.write_text(stderr_src.read_text(errors="ignore"))
                session_output.update_manifest(run_dir, {"validation_stderr": str(stderr_dest)})
            except OSError:
                logger.debug("Failed to write validation stderr for %s", run_dir)


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


def get_issue_number() -> Optional[int]:
    """Get issue number from environment.

    The orchestrator sets ORCHESTRATOR_ISSUE_NUMBER when launching agents.
    Returns None if not set (standalone usage).
    """
    issue_str = os.environ.get("ORCHESTRATOR_ISSUE_NUMBER")
    if issue_str:
        try:
            return int(issue_str)
        except ValueError:
            return None
    return None


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


def format_comment_body(status: str, args: argparse.Namespace) -> str:  # noqa: C901 - distinct markdown templates for each status type
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

    # Orchestrator tells agent where to write via env var
    # This ensures each session type writes to a distinct file
    base_path = get_env("COMPLETION_PATH") or COMPLETION_RECORD_PATH
    output_path = worktree_root / base_path

    # Create all necessary parent directories
    output_path.parent.mkdir(parents=True, exist_ok=True)

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


def load_validation_cmd(worktree: Path) -> tuple[Optional[str], int]:
    """Load validation configuration from the worktree's config file.

    Reads from .issue-orchestrator/config/ in the worktree.
    This ensures tests are deterministic - no env var leakage from parent processes.

    Args:
        worktree: Path to the worktree root

    Returns:
        Tuple of (command, timeout_seconds) or (None, 0) if not configured
    """
    from ...infra.config import load_validation_config

    # Read validation config from the worktree's config file
    validation_config = load_validation_config(worktree)

    cmd = validation_config.get("cmd")
    if cmd:
        return cmd, validation_config.get("timeout_seconds", 300)

    return None, 0


def run_validation(
    worktree: Path,
    session_output_dir: Path,
    verbose: bool = False,
) -> Optional[AgentGateResult]:
    """Run validation if configured.

    Args:
        worktree: Path to the worktree root
        session_output_dir: Directory to write validation output
        verbose: Whether to print validation output

    Returns:
        AgentGateResult if validation was run, None if not configured
    """
    cmd, timeout = load_validation_cmd(worktree)
    if not cmd:
        return None

    if verbose:
        print(f"Running validation: {cmd}")

    from ...execution import LocalCommandRunner, GitWorkingCopy

    gate = AgentGate(
        worktree,
        command_runner=LocalCommandRunner(),
        working_copy=GitWorkingCopy(),
        command=cmd,
        timeout_seconds=timeout,
    )
    result = gate.run(session_output_dir=session_output_dir)

    if verbose:
        if result.passed:
            print(f"✓ Validation passed: {result.reason}")
        else:
            print(f"✗ Validation failed: {result.reason}")

    return result


def trigger_orchestrator_resume(verbose: bool = False) -> tuple[bool, str | None]:
    """Trigger the orchestrator to resume processing for this issue.

    This is used after writing completion.json in a debug session to tell
    the orchestrator to pick up the completion and continue the flow
    (create PR, run review, etc.).

    Requires ORCHESTRATOR_API_PORT and ORCHESTRATOR_ISSUE_NUMBER env vars,
    which are set automatically when launching debug sessions from the web UI.

    Args:
        verbose: Whether to print status messages

    Returns:
        Tuple of (success, error_message)
    """
    import urllib.request
    import urllib.error

    port = os.environ.get("ORCHESTRATOR_API_PORT")
    issue_number = os.environ.get("ORCHESTRATOR_ISSUE_NUMBER")

    if not port or not issue_number:
        missing = []
        if not port:
            missing.append("ORCHESTRATOR_API_PORT")
        if not issue_number:
            missing.append("ORCHESTRATOR_ISSUE_NUMBER")
        return False, (
            f"Cannot resume: missing environment variables: {', '.join(missing)}. "
            f"Completion record written. Resume processing from the web UI."
        )

    url = f"http://localhost:{port}/api/issues/{issue_number}/resume"

    if verbose:
        print(f"Triggering orchestrator resume for issue #{issue_number}...")

    try:
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
            if result.get("success"):
                return True, None
            else:
                return False, result.get("error", "Unknown error from orchestrator")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
            error_data = json.loads(body)
            return False, error_data.get("error", f"HTTP {e.code}: {body}")
        except Exception:
            return False, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return False, f"Could not reach orchestrator API: {e}"
    except Exception as e:
        return False, f"Resume request failed: {e}"


def run_preflight_push_check(worktree: Path, verbose: bool = False) -> tuple[bool, str | None, str | None]:
    """Check if git push would succeed by calling the orchestrator API.

    The agent environment has git credentials scrubbed, so we can't do this
    check directly. Instead, we call the orchestrator's API which has credentials.

    Args:
        worktree: Path to the worktree root
        verbose: Whether to print status messages

    Returns:
        Tuple of (would_succeed, error_message, fix_hint)
    """
    import urllib.request
    import urllib.error

    # Get orchestrator port from environment
    port = os.environ.get("ORCHESTRATOR_API_PORT")
    if not port:
        # No port configured - skip preflight check
        # This happens when running agent-done outside orchestrator context
        if verbose:
            print("Note: ORCHESTRATOR_API_PORT not set, skipping push preflight check")
        return True, None, None

    url = f"http://localhost:{port}/api/preflight-push"
    payload = json.dumps({"worktree": str(worktree)}).encode("utf-8")

    if verbose:
        print(f"Checking if push would succeed...")

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
            return (
                result.get("would_succeed", False),
                result.get("error"),
                result.get("fix_hint"),
            )
    except urllib.error.URLError as e:
        # Can't reach orchestrator - skip check (maybe running standalone)
        if verbose:
            print(f"Note: Could not reach orchestrator API: {e}")
        return True, None, None
    except Exception as e:
        if verbose:
            print(f"Note: Preflight check failed: {e}")
        return True, None, None


def main() -> None:  # noqa: C901, PLR0912 - CLI entry point with argument parsing, validation, preflight, and status-specific handling
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Complete agent work with structured status reporting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
  Completed successfully:
    agent-done completed --implementation "Added user auth" --problems "None"

  Completed with resume (debug session):
    agent-done completed --implementation "Fixed the bug" --problems "None" --resume

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
        "--verbose", "-v",
        action="store_true",
        help="Show detailed output",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="After writing completion, trigger orchestrator to resume processing. "
             "Requires ORCHESTRATOR_API_PORT and ORCHESTRATOR_ISSUE_NUMBER env vars "
             "(set automatically when launching debug sessions from the web UI).",
    )

    args = parser.parse_args()
    status = args.status
    issue_number = get_issue_number()

    # Log start of agent-done
    if issue_number:
        logger.info(issue_log(issue_number, "agent-done starting: status=%s"), status)
    else:
        logger.info("[agent-done] Starting (standalone): status=%s", status)

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

    # Run validation if configured (and not skipped)
    # Skip validation for blocked/needs_human - the agent is already reporting a problem
    validation_result = None
    statuses_requiring_validation = {
        AgentStatus.COMPLETED,
        AgentStatus.APPROVED,
        AgentStatus.CHANGES_REQUESTED,
    }
    should_validate = status in statuses_requiring_validation

    if should_validate:
        # Check if validation is configured before requiring session output
        validation_cmd, _ = load_validation_cmd(worktree_root)
        if validation_cmd:
            # Get session output dir for validation to write directly there
            if not record.session_id:
                logger.error("[agent-done] Validation requires session_id but none found")
                sys.exit(1)
            session_output_helper = FileSystemSessionOutput()
            session_output_dir = session_output_helper.find_run_dir(
                worktree_root, session_name=record.session_id
            )
            if session_output_dir is None:
                logger.error(
                    "[agent-done] Validation requires session output dir but not found for %s",
                    record.session_id,
                )
                sys.exit(1)
            validation_result = run_validation(
                worktree_root,
                session_output_dir=session_output_dir,
                verbose=args.verbose,
            )
        if validation_result and not validation_result.passed:
            _record_validation_artifacts(worktree_root, record.session_id, validation_result)
            print(f"\n{'='*60}")
            print("❌ VALIDATION FAILED - agent-done cannot complete")
            print(f"{'='*60}")
            print(f"\nReason: {validation_result.reason}")

            # Print actual error output so Claude can see what failed
            if validation_result.record and validation_result.record.stderr_path:
                stderr_path = Path(validation_result.record.stderr_path)
                if stderr_path.exists():
                    stderr_content = stderr_path.read_text()
                    if stderr_content.strip():
                        print(f"\n--- STDERR (what failed) ---")
                        # Show last 50 lines to keep it manageable
                        lines = stderr_content.strip().split('\n')
                        if len(lines) > 50:
                            print(f"... ({len(lines) - 50} lines truncated)")
                            lines = lines[-50:]
                        print('\n'.join(lines))
                        print("--- END STDERR ---")

            if validation_result.record and validation_result.record.stdout_path:
                stdout_path = Path(validation_result.record.stdout_path)
                if stdout_path.exists():
                    stdout_content = stdout_path.read_text()
                    if stdout_content.strip():
                        print(f"\n--- STDOUT ---")
                        lines = stdout_content.strip().split('\n')
                        if len(lines) > 30:
                            print(f"... ({len(lines) - 30} lines truncated)")
                            lines = lines[-30:]
                        print('\n'.join(lines))
                        print("--- END STDOUT ---")

            # Print paths to full output files (only if they exist)
            output_paths = []
            if validation_result.record and validation_result.record.stderr_path:
                if Path(validation_result.record.stderr_path).exists():
                    output_paths.append(f"  stderr: {validation_result.record.stderr_path}")
            if validation_result.record and validation_result.record.stdout_path:
                if Path(validation_result.record.stdout_path).exists():
                    output_paths.append(f"  stdout: {validation_result.record.stdout_path}")
            if output_paths:
                print(f"\nFull output saved to:")
                for path in output_paths:
                    print(path)

            print(f"\n{'='*60}")
            print("TO FIX: Read the errors above, fix them, then run agent-done again.")
            print("If you CANNOT fix after 2-3 attempts, use:")
            print('  agent-done blocked --reason "Validation failing: <error>" --attempted "..."')
            print(f"{'='*60}")

            # Log validation failure
            if issue_number:
                logger.info(issue_log(issue_number, "agent-done outcome: status=%s validation=FAILED"), status)
            else:
                logger.info("[agent-done] Outcome (standalone): status=%s validation=FAILED", status)

            sys.exit(1)
    elif status in {AgentStatus.BLOCKED, AgentStatus.NEEDS_HUMAN}:
        # Log that we're skipping validation for these statuses
        status_name = status.value if hasattr(status, 'value') else status
        print(f"Note: Skipping validation for '{status_name}' status (agent is reporting a problem)")

    # If validation ran, include the record path in the completion record
    if validation_result and validation_result.record_path:
        record.validation_record_path = validation_result.record_path

    # Run preflight push check for statuses that will push
    # This catches issues like branch divergence while the agent can still fix them
    statuses_that_push = {AgentStatus.COMPLETED, AgentStatus.BLOCKED, AgentStatus.NEEDS_HUMAN}
    if status in statuses_that_push:
        would_succeed, error, fix_hint = run_preflight_push_check(worktree_root, verbose=args.verbose)
        if not would_succeed:
            print(f"\n{'='*60}")
            print("❌ PUSH WOULD FAIL - agent-done cannot complete")
            print(f"{'='*60}")
            print(f"\nError: {error}")
            if fix_hint:
                print(f"\nTo fix: {fix_hint}")
            print(f"\n{'='*60}")
            print("Fix the issue above, then run agent-done again.")
            print(f"{'='*60}")

            if issue_number:
                logger.info(issue_log(issue_number, "agent-done outcome: status=%s push_preflight=FAILED"), status)
            else:
                logger.info("[agent-done] Outcome (standalone): status=%s push_preflight=FAILED", status)

            sys.exit(1)

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

    # Handle --resume flag: trigger orchestrator to process completion
    if args.resume:
        print("\nTriggering orchestrator resume...")
        resume_success, resume_error = trigger_orchestrator_resume(verbose=args.verbose)
        if resume_success:
            print("Orchestrator resume triggered successfully.")
            print("The orchestrator will now process this completion and continue the flow.")
        else:
            print(f"\n{resume_error}")
            # Don't exit with error - completion was written successfully,
            # user can still use web UI to resume
    else:
        print("\nThe orchestrator will process this record and perform the necessary actions.")

    # Log successful outcome
    if issue_number:
        logger.info(issue_log(issue_number, "agent-done outcome: status=%s validation=%s resume=%s"),
                   status, "passed" if validation_result and validation_result.passed else "skipped",
                   "triggered" if args.resume else "not_requested")
    else:
        logger.info("[agent-done] Outcome (standalone): status=%s resume=%s", status,
                   "triggered" if args.resume else "not_requested")


def write_error_completion(error_msg: str, status: str) -> Optional[Path]:
    """Write an error completion record when agent-done itself fails.

    This ensures the orchestrator knows something went wrong even if
    agent-done crashes after the AI agent called it.
    """
    try:
        worktree_root = find_worktree_root()
        base_path = get_env("COMPLETION_PATH") or COMPLETION_RECORD_PATH
        output_path = worktree_root / base_path

        error_record = {
            "outcome": status,  # Preserve what the agent intended
            "agent_done_error": error_msg,
            "session_id": get_session_id(),
            "timestamp": datetime.now().isoformat(),
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(error_record, indent=2))
        return output_path
    except Exception:
        # If we can't even write the error record, there's nothing more we can do
        return None


def safe_main() -> None:
    """Wrapper around main() that catches unexpected errors.

    If agent-done crashes after the AI agent called it, we still want to
    write a completion record so the orchestrator knows what happened.
    The AI agent did its job - it called agent-done. Infrastructure failure
    after that is not the AI's fault.
    """
    status = "unknown"
    issue_number = get_issue_number()

    try:
        # Parse just enough to get status for error reporting
        if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
            status = sys.argv[1]
        main()
    except SystemExit:
        # Normal exit (including validation failures) - don't intercept
        raise
    except Exception as e:
        # Unexpected error - write error completion so orchestrator knows
        error_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

        if issue_number:
            logger.error(issue_log(issue_number, "agent-done crashed: %s"), str(e))
        else:
            logger.error("[agent-done] Crashed (standalone): %s", str(e))

        print(f"\n{'='*60}", file=sys.stderr)
        print("❌ AGENT-DONE INTERNAL ERROR", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        print(f"\nError: {e}", file=sys.stderr)
        print(f"\n{traceback.format_exc()}", file=sys.stderr)

        # Try to write error completion
        error_path = write_error_completion(error_msg, status)
        if error_path:
            print(f"\nError completion written to: {error_path}", file=sys.stderr)
            print("The orchestrator will mark this issue as blocked.", file=sys.stderr)
        else:
            print("\nCould not write error completion record.", file=sys.stderr)

        sys.exit(1)


if __name__ == "__main__":
    safe_main()
