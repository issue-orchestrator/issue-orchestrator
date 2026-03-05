"""Coding agent completion CLI.

Used by coding and rework agents to signal completion. Enforces:
- Dirty-file check (working tree must be clean)
- Validation gate (tests/linting if configured)
- Preflight push check

Review agents use reviewer-done instead.
"""

import argparse
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

from .agent_done import (
    AgentStatus,
    FileSystemSessionOutput,
    build_completion_record,
    find_worktree_root,
    get_issue_number,
    load_validation_cmd,
    run_preflight_push_check,
    run_validation,
    trigger_orchestrator_resume,
    validate_fields,
    write_completion_record,
    write_error_completion,
    write_marker_file,
    record_validation_artifacts,
)
from ...infra.env import get_env
from ...infra.logging_config import issue_log

import logging

logger = logging.getLogger(__name__)

CODING_STATUSES = [
    AgentStatus.COMPLETED,
    AgentStatus.BLOCKED,
    AgentStatus.NEEDS_HUMAN,
]


def check_dirty_files() -> list[str]:
    """Check for uncommitted files in the working tree.

    Returns list of dirty file paths, or empty list if clean.
    Excludes ``.issue-orchestrator/`` paths (runtime artifacts created
    by the orchestrator session infrastructure, not agent work).
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []  # Can't determine — don't block
        lines = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
        # Filter out orchestrator runtime artifacts (session logs, manifests, etc.)
        # These are created by create_session() before the agent command runs.
        return [line for line in lines if ".issue-orchestrator/" not in line]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []  # Can't determine — don't block


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser for coding-done."""
    parser = argparse.ArgumentParser(
        prog="coding-done",
        description="Complete coding/rework agent work with structured status reporting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
  Completed successfully:
    coding-done completed --implementation "Added user auth" --problems "None"

  Completed with resume (debug session):
    coding-done completed --implementation "Fixed the bug" --problems "None" --resume

  Blocked:
    coding-done blocked --reason "Need API credentials" --attempted "Checked env vars"

  Need human input:
    coding-done needs_human --question "Should we use OAuth or API keys?"

STATUSES:
  completed    - Work done, PR ready (requires: --implementation, --problems)
  blocked      - Cannot proceed (requires: --reason, --attempted)
  needs_human  - Need decision (requires: --question)
"""
    )

    parser.add_argument(
        "status",
        choices=["completed", "blocked", "needs_human"],
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

    # PR options
    parser.add_argument("--pr-labels", nargs="+", help="Extra labels to add to the PR")

    # Meta options
    parser.add_argument("--dry-run", action="store_true", help="Show what would be written")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="After writing completion, trigger orchestrator to resume processing.",
    )

    return parser


def main() -> None:  # noqa: C901, PLR0912
    """Main entry point for coding-done."""
    parser = build_parser()
    args = parser.parse_args()
    status = args.status
    issue_number = get_issue_number()

    if issue_number:
        logger.info(issue_log(issue_number, "coding-done starting: status=%s"), status)
    else:
        logger.info("[coding-done] Starting (standalone): status=%s", status)

    # 1. Validate required fields
    validate_fields(status, args)

    # Build completion record
    record = build_completion_record(status, args)

    if args.dry_run:
        print("--- DRY RUN: Would write this completion record ---")
        print(json.dumps(record.to_dict(), indent=2))
        print("--- END ---")
        return

    worktree_root = find_worktree_root()

    # 2. Check for dirty files (coding agents must commit everything)
    dirty_files = check_dirty_files()
    if dirty_files:
        print(f"\n{'='*60}")
        print("❌ WORKING TREE IS DIRTY — coding-done cannot complete")
        print(f"{'='*60}")
        print(f"\nUncommitted files ({len(dirty_files)}):")
        for f in dirty_files[:15]:
            print(f"  {f}")
        if len(dirty_files) > 15:
            print(f"  ... and {len(dirty_files) - 15} more")
        print(f"\nCommit all changes before calling coding-done.")
        print("If you modified contracts or schemas, regenerate artifacts first.")
        print(f"{'='*60}")

        if issue_number:
            logger.info(issue_log(issue_number, "coding-done outcome: status=%s dirty_files=%d"), status, len(dirty_files))

        sys.exit(1)

    # 3. Run validation if configured
    #    When running under the orchestrator, skip validation here — the orchestrator
    #    runs its own validation gate after the session completes, with retry logic.
    #    Running validation inside coding-done is redundant and eats into the session
    #    timeout, leaving no time for the agent to recover from failures.
    validation_result = None
    under_orchestrator = bool(get_env("SESSION_ID") or os.environ.get("ORCHESTRATOR_SESSION_ID"))
    statuses_requiring_validation = {AgentStatus.COMPLETED}
    if status in statuses_requiring_validation and not under_orchestrator:
        validation_cmd, _ = load_validation_cmd(worktree_root)
        if validation_cmd:
            if not record.session_id:
                logger.error("[coding-done] Validation requires session_id but none found")
                sys.exit(1)
            session_output_helper = FileSystemSessionOutput()
            session_output_dir = session_output_helper.find_run_dir(
                worktree_root, session_name=record.session_id
            )
            if session_output_dir is None:
                logger.error("[coding-done] Validation requires session output dir but not found for %s", record.session_id)
                sys.exit(1)
            validation_result = run_validation(worktree_root, session_output_dir=session_output_dir, verbose=args.verbose)
    elif status in statuses_requiring_validation and under_orchestrator:
        if args.verbose:
            print("Skipping validation (orchestrator will run validation gate after session)")
    elif status in {AgentStatus.BLOCKED, AgentStatus.NEEDS_HUMAN}:
        print(f"Note: Skipping validation for '{status}' status (agent is reporting a problem)")

    if validation_result and not validation_result.passed:
        record_validation_artifacts(worktree_root, record.session_id, validation_result)
        print(f"\n{'='*60}")
        print("❌ VALIDATION FAILED — coding-done cannot complete")
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
        print("TO FIX: Read the errors above, fix them, then run coding-done again.")
        print("If you CANNOT fix after 2-3 attempts, use:")
        print('  coding-done blocked --reason "Validation failing: <error>" --attempted "..."')
        print(f"{'='*60}")

        if issue_number:
            logger.info(issue_log(issue_number, "coding-done outcome: status=%s validation=FAILED"), status)
        sys.exit(1)

    if validation_result and validation_result.record_path:
        record.validation_record_path = validation_result.record_path

    # 4. Run preflight push check
    statuses_that_push = {AgentStatus.COMPLETED, AgentStatus.BLOCKED, AgentStatus.NEEDS_HUMAN}
    if status in statuses_that_push:
        would_succeed, error, fix_hint = run_preflight_push_check(worktree_root, verbose=args.verbose)
        if not would_succeed:
            print(f"\n{'='*60}")
            print("❌ PUSH WOULD FAIL — coding-done cannot complete")
            print(f"{'='*60}")
            print(f"\nError: {error}")
            if fix_hint:
                print(f"\nTo fix: {fix_hint}")
            print(f"\n{'='*60}")
            print("Fix the issue above, then run coding-done again.")
            print(f"{'='*60}")

            if issue_number:
                logger.info(issue_log(issue_number, "coding-done outcome: status=%s push_preflight=FAILED"), status)
            sys.exit(1)

    # 5. Write marker + completion record
    write_marker_file(status)
    output_path = write_completion_record(record)
    output_path_abs = output_path.resolve()

    print(f"Completion record written to: {output_path_abs}")
    print(f"Status: {status}")
    print(f"Session: {record.session_id}")
    if validation_result:
        print(f"Validation: {'passed' if validation_result.passed else 'failed'}")

    # Handle --resume flag
    if args.resume:
        print("\nTriggering orchestrator resume...")
        resume_success, resume_error = trigger_orchestrator_resume(verbose=args.verbose)
        if resume_success:
            print("Orchestrator resume triggered successfully.")
        else:
            print(f"\n{resume_error}")
    else:
        print("\nThe orchestrator will process this record and perform the necessary actions.")

    if issue_number:
        logger.info(
            issue_log(issue_number, "coding-done outcome: status=%s validation=%s resume=%s"),
            status,
            "passed" if validation_result and validation_result.passed else "skipped",
            "triggered" if args.resume else "not_requested",
        )


def safe_main() -> None:
    """Wrapper that catches unexpected errors and writes error completion."""
    status = "unknown"
    issue_number = get_issue_number()

    try:
        if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
            status = sys.argv[1]
        main()
    except SystemExit:
        raise
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

        if issue_number:
            logger.error(issue_log(issue_number, "coding-done crashed: %s"), str(e))

        print(f"\n{'='*60}", file=sys.stderr)
        print("❌ CODING-DONE INTERNAL ERROR", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        print(f"\nError: {e}", file=sys.stderr)
        print(f"\n{traceback.format_exc()}", file=sys.stderr)

        error_path = write_error_completion(error_msg, status)
        if error_path:
            print(f"\nError completion written to: {error_path}", file=sys.stderr)

        sys.exit(1)


if __name__ == "__main__":
    safe_main()
