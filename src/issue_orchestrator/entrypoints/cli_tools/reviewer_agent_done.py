"""Reviewer agent completion CLI - writes review verdict without validation.

This is the reviewer's sanctioned completion command. It:
- Records the review verdict (approved or changes_requested)
- Does NOT run validation (the coder already validated)
- Does NOT push code or mutate PRs (orchestrator handles that)

Usage:
    reviewer-agent-done approved --summary "Code is clean" --risk low
    reviewer-agent-done changes_requested --issues "Missing error handling" --risk medium
"""

import argparse
import json
import sys

from .agent_done import (
    build_completion_record,
    get_issue_number,
    validate_fields,
    write_completion_record,
    write_error_completion,
    write_marker_file,
)
from ...infra.logging_config import issue_log

import logging
import traceback

logger = logging.getLogger(__name__)


def main() -> None:
    """Reviewer agent-done entry point."""
    parser = argparse.ArgumentParser(
        description="Complete review with structured verdict (no validation).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
  Approve:
    reviewer-agent-done approved --summary "Code is clean and well-tested" --risk low

  Request changes:
    reviewer-agent-done changes_requested --issues "Missing error handling in auth.py" --risk medium
""",
    )

    parser.add_argument(
        "status",
        choices=["approved", "changes_requested"],
        help="Review verdict",
    )

    # Review fields
    parser.add_argument("--summary", "-s", help="Summary of review (for approved)")
    parser.add_argument("--issues", help="Issues found (for changes_requested)")
    parser.add_argument("--risk", choices=["low", "medium", "high"], help="Risk level")
    parser.add_argument("--checks", nargs="+", help="Checks that passed (for approved)")
    parser.add_argument("--checks-needed", nargs="+", help="Checks needed (for changes_requested)")

    # Meta options
    parser.add_argument("--dry-run", action="store_true", help="Show what would be written")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")

    args = parser.parse_args()
    status = args.status
    issue_number = get_issue_number()

    if issue_number:
        logger.info(issue_log(issue_number, "reviewer-agent-done starting: status=%s"), status)
    else:
        logger.info("[reviewer-agent-done] Starting (standalone): status=%s", status)

    validate_fields(status, args)

    record = build_completion_record(status, args)

    if args.dry_run:
        print("--- DRY RUN: Would write this completion record ---")
        print(json.dumps(record.to_dict(), indent=2))
        print("--- END ---")
        return

    # No validation for reviewers - the coder already validated
    # No preflight push check - reviewers don't push code

    write_marker_file(status)
    output_path = write_completion_record(record)

    print(f"Review verdict written to: {output_path.resolve()}")
    print(f"Verdict: {status}")
    print("\nThe orchestrator will process this verdict.")

    if issue_number:
        logger.info(
            issue_log(issue_number, "reviewer-agent-done outcome: status=%s"),
            status,
        )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        write_error_completion(traceback.format_exc(), "approved")
        sys.exit(1)
