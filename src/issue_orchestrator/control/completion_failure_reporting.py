"""Failure comments and diagnostics for completion processing."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..domain.session_run import SessionRunAssets
from ..infra.issue_diagnostics import write_issue_diagnostic
from ..ports.session_output import SessionOutput
from .completion_types import REVIEW_EXCHANGE_ERROR_PREFIX

logger = logging.getLogger(__name__)


def build_review_exchange_recovery_note(errors: list[str]) -> str | None:
    """Return recovery guidance when a failure came from the review exchange.

    A review-exchange failure happens *after* the coder's work has been
    validated and committed locally, so the generic "publish/finalize failed"
    comment reads as a coding failure when it is really a finalization-stage
    failure. Give the operator a recovery-specific explanation instead (#6659):
    the committed work is safe, a retry recovers finalization rather than
    re-coding, and tracked runtime artifacts are the likely culprit.
    """
    review_exchange_errors = [
        error for error in errors if error.startswith(REVIEW_EXCHANGE_ERROR_PREFIX)
    ]
    if not review_exchange_errors:
        return None

    lines = [
        "### Recovery: review-exchange finalization failed",
        "",
        "This failed during review-exchange finalization, **after** the coder's "
        "work was validated and committed locally — it is not a coding failure. "
        "The committed branch work is safe.",
        "",
        "- Retry recovers finalization; it should not require re-coding from scratch.",
        "- The likely cause is issue-orchestrator runtime artifacts committed onto "
        "the branch (for example under `.issue-orchestrator/persistent-pairs/` or "
        "`.issue-orchestrator/review-exchange-turn-prompt.md`), which break the "
        "reviewer-worktree fast-forward checkout. Remove them with "
        "`git rm --cached` and confirm they are gitignored.",
    ]
    return "\n".join(lines)


def build_cleanup_failure_comment(
    *,
    issue_number: int,
    worktree: Path,
    record_path: Path,
) -> str:
    """Build a cleanup failure comment with a local diagnostic reference."""
    diagnostic = write_issue_diagnostic(
        worktree=worktree,
        issue_number=issue_number,
        kind="completion-cleanup",
        summary="Completion record could not be deleted",
        details={
            "record_path": str(record_path),
            "worktree": str(worktree),
        },
    )

    if diagnostic:
        return (
            "WARNING: Cleanup incomplete\n\n"
            "The completion record could not be deleted after processing. "
            "This can happen if the file is still open or locked.\n\n"
            f"- Worktree: `{diagnostic.worktree_name}`\n"
            f"- Diagnostic file: `{diagnostic.relative_path}`\n\n"
            "Close any editors or processes using the file, then delete it manually."
        )
    return (
        "WARNING: Cleanup incomplete\n\n"
        "The completion record could not be deleted after processing. "
        "Close any editors or processes using the file, then delete it manually."
    )


def build_gate_failure_comment(
    *,
    gate_reason: str,
    validation_failed_label: str,
) -> str:
    """Build the GitHub comment body for publish gate failures."""
    return (
        "## Validation Failed\n\n"
        "Publish actions were blocked by validation.\n\n"
        f"- Reason: {gate_reason}\n"
        f"- Label added: `{validation_failed_label}`\n"
    )


def build_processing_failure_comment(
    *,
    errors: list[str],
    actions_taken: list[str],
    diagnostic_path: str | None,
) -> str:
    """Build the GitHub comment body for completion processing failures."""
    primary_error = errors[0] if errors else "Unknown processing error"
    comment = (
        "## Orchestrator Processing Failed\n\n"
        "The agent reported completion, but orchestrator publish/finalize steps failed.\n\n"
        f"- Primary error: {primary_error}\n"
    )
    if actions_taken:
        comment += f"- Actions completed before failure: {', '.join(actions_taken)}\n"
    if diagnostic_path:
        comment += f"- Diagnostic file: `{diagnostic_path}`\n"
    recovery_note = build_review_exchange_recovery_note(errors)
    if recovery_note:
        comment += f"\n{recovery_note}\n"
    return comment


def write_failure_diagnostic(
    *,
    session_output: SessionOutput,
    worktree: Path,
    session_name: str | None,
    issue_number: int,
    issue_title: str,
    branch: str | None,
    outcome: str,
    requested_actions: list[str],
    actions_taken: list[str],
    errors: list[str],
    error_details: list[dict[str, Any]],
    duration_seconds: float,
    run_assets: SessionRunAssets,
) -> str | None:
    """Write detailed failure diagnostics to a file in the worktree."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"failure-diagnostic-{timestamp}.json"
    diagnostic_artifact = run_assets.diagnostic_artifact(filename)
    diagnostic_dir = run_assets.run_dir
    diagnostic_rel = f".issue-orchestrator/sessions/{run_assets.run_dir.name}/{filename}"

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_name": session_name,
        "issue_number": issue_number,
        "issue_title": issue_title,
        "branch": branch,
        "worktree": str(worktree),
        "outcome_reported": outcome,
        "requested_actions": requested_actions,
        "actions_taken": actions_taken,
        "errors": errors,
        "error_details": error_details,
        "duration_seconds": round(duration_seconds, 2),
    }

    try:
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        diagnostic_artifact.path.write_text(json.dumps(payload, indent=2))
        session_output.update_manifest(
            run_assets.run_dir,
            {"diagnostic_path": diagnostic_rel},
        )
        logger.info(
            "[DIAGNOSTIC] Wrote failure diagnostic: issue=%d path=%s",
            issue_number, diagnostic_artifact.path,
        )
        # Return relative path for inclusion in GitHub comment
        return diagnostic_rel
    except Exception as exc:
        logger.warning(
            "[DIAGNOSTIC] Failed to write failure diagnostic: issue=%d error=%s",
            issue_number, exc,
        )
        return None
