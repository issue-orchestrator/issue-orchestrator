"""Coder agent completion CLI - writes structured completion record with validation.

This is the coder's sanctioned completion command. It:
- Validates the coder's work (runs configured validation command)
- Writes a structured completion record
- Does NOT push code or create PRs (orchestrator handles that)

Usage:
    coder-agent-done completed --implementation "Added auth" --problems "None"
    coder-agent-done blocked --reason "Need API keys" --attempted "Checked env"
    coder-agent-done needs_human --question "OAuth or API keys?"
"""

import argparse
import json
import sys

from .agent_done import (
    AgentStatus,
    build_completion_record,
    find_worktree_root,
    get_issue_number,
    load_validation_cmd,
    run_validation,
    validate_fields,
    write_completion_record,
    write_error_completion,
    write_marker_file,
    _record_validation_artifacts,
)
from ...execution.session_output_adapter import FileSystemSessionOutput
from ...infra.logging_config import issue_log

import logging
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)

CODER_STATUSES = [AgentStatus.COMPLETED, AgentStatus.BLOCKED, AgentStatus.NEEDS_HUMAN]


def main() -> None:  # noqa: C901, PLR0912
    """Coder agent-done entry point."""
    parser = argparse.ArgumentParser(
        description="Complete coder work with validation and structured reporting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
  Completed successfully:
    coder-agent-done completed --implementation "Added user auth" --problems "None"

  Blocked:
    coder-agent-done blocked --reason "Need API credentials" --attempted "Checked env vars"

  Need human input:
    coder-agent-done needs_human --question "Should we use OAuth or API keys?"
""",
    )

    parser.add_argument(
        "status",
        choices=["completed", "blocked", "needs_human"],
        help="Completion status",
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

    # PR options
    parser.add_argument(
        "--pr-labels", nargs="+",
        help="Extra labels to add to the PR",
    )

    # Meta options
    parser.add_argument("--dry-run", action="store_true", help="Show what would be written")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")

    args = parser.parse_args()
    status = args.status
    issue_number = get_issue_number()

    if issue_number:
        logger.info(issue_log(issue_number, "coder-agent-done starting: status=%s"), status)
    else:
        logger.info("[coder-agent-done] Starting (standalone): status=%s", status)

    validate_fields(status, args)

    # Build completion record
    record = build_completion_record(status, args)

    if args.dry_run:
        print("--- DRY RUN: Would write this completion record ---")
        print(json.dumps(record.to_dict(), indent=2))
        print("--- END ---")
        return

    worktree_root = find_worktree_root()

    # Run validation for completed status
    validation_result = None
    if status == AgentStatus.COMPLETED:
        validation_cmd, _ = load_validation_cmd(worktree_root)
        if validation_cmd:
            if not record.session_id:
                logger.error("[coder-agent-done] Validation requires session_id")
                sys.exit(1)
            session_output_helper = FileSystemSessionOutput()
            session_output_dir = session_output_helper.find_run_dir(
                worktree_root, session_name=record.session_id,
            )
            if session_output_dir is None:
                logger.error("[coder-agent-done] Session output dir not found for %s", record.session_id)
                sys.exit(1)
            validation_result = run_validation(
                worktree_root,
                session_output_dir=session_output_dir,
                verbose=args.verbose,
            )
        if validation_result and not validation_result.passed:
            _record_validation_artifacts(worktree_root, record.session_id, validation_result)
            print(f"\n{'='*60}")
            print("VALIDATION FAILED - coder-agent-done cannot complete")
            print(f"{'='*60}")
            print(f"\nReason: {validation_result.reason}")

            if validation_result.record and validation_result.record.stderr_path:
                stderr_path = Path(validation_result.record.stderr_path)
                if stderr_path.exists():
                    stderr_content = stderr_path.read_text()
                    if stderr_content.strip():
                        print(f"\n--- STDERR (what failed) ---")
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

            print(f"\n{'='*60}")
            print("TO FIX: Read the errors above, fix them, then run coder-agent-done again.")
            print("If you CANNOT fix after 2-3 attempts, use:")
            print('  coder-agent-done blocked --reason "Validation failing: <error>" --attempted "..."')
            print(f"{'='*60}")

            if issue_number:
                logger.info(issue_log(issue_number, "coder-agent-done: validation=FAILED"))
            sys.exit(1)
    elif status in {AgentStatus.BLOCKED, AgentStatus.NEEDS_HUMAN}:
        print(f"Note: Skipping validation for '{status}' status")

    if validation_result and validation_result.record_path:
        record.validation_record_path = validation_result.record_path

    # No preflight push check - the orchestrator handles pushing and has its
    # own error handling. A dry-run push here would trigger the pre-push hook
    # (which runs validation), causing duplicate/conflicting validation runs.

    write_marker_file(status)
    output_path = write_completion_record(record)

    print(f"Completion record written to: {output_path.resolve()}")
    print(f"Status: {status}")
    if validation_result:
        print(f"Validation: {'passed' if validation_result.passed else 'failed'}")
    print("\nThe orchestrator will process this record and perform the necessary actions.")

    if issue_number:
        logger.info(
            issue_log(issue_number, "coder-agent-done outcome: status=%s validation=%s"),
            status, "passed" if validation_result and validation_result.passed else "skipped",
        )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        write_error_completion(traceback.format_exc(), "completed")
        sys.exit(1)
