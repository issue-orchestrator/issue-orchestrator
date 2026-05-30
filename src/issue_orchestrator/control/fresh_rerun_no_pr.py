"""Fresh-rerun helpers for approved no-PR outcomes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..domain.fresh_lifecycle_rerun import manifest_has_fresh_lifecycle_rerun
from ..domain.models import RequestedAction, Session
from ..ports.session_output import SessionOutput
from .actions import Action, AddCommentAction, AddLabelAction, CloseIssueAction
from .completion_pr_collision import is_no_commits_error
from .reconciliation import build_expected_for_mutation
from .review_exchange_modes import is_final_review_exchange_mode

logger = logging.getLogger(__name__)

_NO_PR_COMMENT = (
    "**Fresh Lifecycle Rerun Complete**\n\n"
    "The rerun completed coding verification, validation, and fresh review "
    "without producing a code diff against the configured base. No PR was "
    "created because there are no changes to publish.\n\n"
    "The issue is being closed to record the successful rerun and prevent it "
    "from being picked up again."
)


def try_recover_fresh_rerun_no_pr(
    session_output: SessionOutput,
    worktree: Path,
    session_name: str | None,
    action: RequestedAction,
    error: Exception,
    exchange_mode: str | None,
    exchange_result: Any | None,
    actions_taken: list[str],
    issue_number: int,
    branch: str | None,
) -> bool:
    """Treat approved fresh reruns with no commits as successful no-PR work."""
    eligible = (
        action == RequestedAction.CREATE_PR
        and is_final_review_exchange_mode(exchange_mode)
        and bool(exchange_result)
        and is_no_commits_error(error)
        and _has_fresh_rerun_intent(session_output, worktree, session_name)
    )
    if not eligible:
        return False

    actions_taken.append(
        "Fresh lifecycle rerun approved with no code changes; skipped PR creation"
    )
    logger.info(
        "[FRESH_RERUN] Skipping PR creation for approved no-diff rerun: "
        "issue=%d branch=%s",
        issue_number,
        branch,
    )
    return True


def final_actions_after_review_exchange(
    *,
    session: Session,
    done: bool,
    pr_url: str | None,
    approved: bool,
    session_output: SessionOutput,
    pending_label: str,
) -> tuple[Action, ...]:
    """Return issue/PR actions after a review exchange has completed."""
    expected = build_expected_for_mutation()
    if approved and pr_url:
        return (
            AddLabelAction(
                issue_number=session.issue.number,
                label=pending_label,
                reason="review exchange completed - awaiting merge",
                expected=expected,
            ),
        )

    ready = done and approved and not pr_url and bool(session.worktree_path)
    if not ready:
        return ()

    worktree = Path(session.worktree_path or "")
    if not _has_fresh_rerun_intent(session_output, worktree, session.terminal_id):
        return ()

    reason = "fresh lifecycle rerun completed without publishable changes"
    return (
        AddCommentAction(
            number=session.issue.number,
            comment=_NO_PR_COMMENT,
            reason=reason,
            expected=expected,
        ),
        CloseIssueAction(
            issue_number=session.issue.number,
            reason=reason,
            expected=expected,
        ),
    )


def _has_fresh_rerun_intent(
    session_output: SessionOutput,
    worktree: Path,
    session_name: str | None,
) -> bool:
    if not session_name:
        return False
    run_dir = session_output.find_run_dir(worktree, session_name)
    if not run_dir:
        return False
    return manifest_has_fresh_lifecycle_rerun(session_output.read_manifest(run_dir))
