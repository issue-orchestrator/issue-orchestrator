"""Review agent completion CLI.

Used by review agents to signal their verdict. Lightweight:
- No dirty-file check (reviewers don't push code)
- No validation gate
- No preflight push check

Coding agents use coding-done instead.
"""

import argparse
import json
import sys
import traceback

from .agent_done import (
    build_completion_record,
    get_issue_number,
    validate_fields,
    write_completion_record,
    write_error_completion,
    write_marker_file,
)
from .orchestrator_resume import trigger_orchestrator_resume
from ...infra.logging_config import issue_log

import logging

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser for reviewer-done."""
    parser = argparse.ArgumentParser(
        prog="reviewer-done",
        description="Complete review agent work with structured verdict reporting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
  Review approved:
    reviewer-done approved --summary "Code is clean" --risk low --checks tests_added

  Review requests changes:
    reviewer-done changes_requested --issues "Missing error handling" --risk medium

STATUSES:
  approved           - Review passed (requires: --summary, --risk)
  changes_requested  - Review needs fixes (requires: --issues, --risk)
"""
    )

    parser.add_argument(
        "status",
        choices=["approved", "changes_requested"],
        help="Review verdict"
    )

    # Reviewer fields
    parser.add_argument("--summary", "-s", help="Summary of review")
    parser.add_argument("--issues", help="Issues found that need fixing")
    parser.add_argument("--risk", choices=["low", "medium", "high"], help="Risk level")
    parser.add_argument("--checks", nargs="+", help="Checks that passed")
    parser.add_argument("--checks-needed", nargs="+", help="Checks that need to be done")

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


def main() -> None:
    """Main entry point for reviewer-done."""
    parser = build_parser()
    args = parser.parse_args()
    status = args.status
    issue_number = get_issue_number()

    if issue_number:
        logger.info(issue_log(issue_number, "reviewer-done starting: status=%s"), status)
    else:
        logger.info("[reviewer-done] Starting (standalone): status=%s", status)

    # 1. Validate required fields
    validate_fields(status, args)

    # 2. Build completion record
    record = build_completion_record(status, args)

    if args.dry_run:
        print("--- DRY RUN: Would write this completion record ---")
        print(json.dumps(record.to_dict(), indent=2))
        print("--- END ---")
        return

    # 3. Write marker + completion record (no validation, no push check)
    write_marker_file(status)
    output_path = write_completion_record(record)
    output_path_abs = output_path.resolve()

    print(f"Completion record written to: {output_path_abs}")
    print(f"Status: {status}")
    print(f"Session: {record.session_id}")

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
            issue_log(issue_number, "reviewer-done outcome: status=%s resume=%s"),
            status,
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
            logger.error(issue_log(issue_number, "reviewer-done crashed: %s"), str(e))

        print(f"\n{'='*60}", file=sys.stderr)
        print("❌ REVIEWER-DONE INTERNAL ERROR", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        print(f"\nError: {e}", file=sys.stderr)
        print(f"\n{traceback.format_exc()}", file=sys.stderr)

        error_path = write_error_completion(error_msg, status)
        if error_path:
            print(f"\nError completion written to: {error_path}", file=sys.stderr)

        sys.exit(1)


if __name__ == "__main__":
    safe_main()
