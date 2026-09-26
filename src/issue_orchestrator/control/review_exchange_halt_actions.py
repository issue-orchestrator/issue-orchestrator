"""What a halted review exchange does to its issue.

Owned apart from ``CompletionActionPlanner`` because the answer depends on who
else holds the issue: recovery, when it already has the run's validated work.
"""

from __future__ import annotations

from ..domain.models import Session
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .label_manager import LabelManager
from .reconciliation import ExpectedState


def review_exchange_halted_actions(
    session: Session,
    expected: ExpectedState,
    labels: LabelManager,
    *,
    recovery_holds_validated_work: bool,
) -> list[Action]:
    """Generate hold actions when a review exchange halts without progress.

    When recovery already holds the run's validated work, recovery owns the
    issue: it blocks it with ``recovery-pending``, publishes the work as a PR
    and routes it to code review, or escalates it itself. Adding
    ``blocked-failed`` on top would veto that review and leave the issue for
    the stuck sweep to escalate, so the halt only reports and releases.
    """
    issue_number = session.issue.number
    header = (
        "⚠️ **Review Exchange Halted**\n\n"
        "The automated review exchange stopped because it could not make further progress.\n\n"
        f"- Session: `{session.terminal_id}`\n"
        f"- Runtime: {session.runtime_minutes:.1f} minutes\n\n"
    )
    release = RemoveLabelAction(
        issue_number=issue_number,
        label=labels.in_progress,
        reason="Review exchange halted - releasing claim",
        expected=expected,
    )
    if recovery_holds_validated_work:
        return [
            AddCommentAction(
                number=issue_number,
                comment=(
                    header
                    + "This run's validated work is held by recovery "
                    f"(`{labels.recovery_pending}`), so this issue is not marked "
                    f"`{labels.blocked_failed}`. Once recovery publishes it, the pull "
                    "request goes to code review. Until then recovery keeps the issue "
                    "blocked, and escalates it if the work cannot be published."
                ),
                reason="Notify that review exchange halted and recovery holds the work",
                expected=expected,
            ),
            release,
        ]
    return [
        AddLabelAction(
            issue_number=issue_number,
            label=labels.blocked_failed,
            reason="Review exchange halted with no progress",
            expected=expected,
        ),
        AddCommentAction(
            number=issue_number,
            comment=(
                header
                + f"This issue has been marked as `{labels.blocked_failed}` and will not be retried automatically.\n"
                "Use Retry/Unblock when you want to run it again."
            ),
            reason="Notify that review exchange halted and issue is on hold",
            expected=expected,
        ),
        release,
    ]
