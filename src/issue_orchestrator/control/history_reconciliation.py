"""Apply-time owner for awaiting-merge history reconciliation.

This boundary keeps the complete terminal-reconciliation policy together:
durable shipped-fix capture happens before process-local history mutation,
the mutation emits its typed timeline facts, and terminal issue runtimes are
released only after the transition succeeds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial

from ..domain.models import AwaitingMergeTerminalStatus, SessionHistoryEntry
from ..domain.triage_session import triage_area_from_labels
from ..events import EventName
from ..ports import EventSink, make_trace_event
from ..ports.triage_authority import TriageAuthorityStore
from .actions import ActionResult, ReconcileHistoryEntryAction
from .session_history import HistoryReconciliationMutation, SessionHistoryOwner

logger = logging.getLogger(__name__)

IssueRuntimeTerminator = Callable[[int, str], object]


def apply_history_reconciliation(
    action: ReconcileHistoryEntryAction,
    *,
    history_owner: SessionHistoryOwner | None,
    events: EventSink,
    triage_authority: TriageAuthorityStore | None,
    terminate_issue_runtime: IssueRuntimeTerminator,
) -> ActionResult:
    """Reconcile one terminal PR fact through all owning boundaries."""
    if history_owner is None:
        return ActionResult.fail(action, "Session history owner is not configured")

    outcome = history_owner.reconcile_awaiting_merge(
        issue_number=action.issue_number,
        pr_url=action.pr_url,
        status=action.status,
        status_reason=action.reason,
        before_transition=_shipped_fix_recorder(action.status, triage_authority),
    )
    if not isinstance(outcome, HistoryReconciliationMutation):
        _log_noop(action, outcome.reason, outcome.current_status)
        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            pr_number=action.pr_number,
            status=action.status,
            noop_reason=outcome.reason,
            current_status=outcome.current_status,
            no_op=True,
        )

    events.publish(make_trace_event(
        EventName.HISTORY_RECONCILED,
        {
            "issue_number": action.issue_number,
            "issue_key": action.issue_key or str(action.issue_number),
            "pr_number": action.pr_number,
            "pr_url": action.pr_url,
            "previous_status": outcome.previous_status,
            "status": outcome.status,
            "status_reason": outcome.status_reason,
            "source": action.source,
        },
    ))
    if outcome.status == "merged":
        events.publish(make_trace_event(
            EventName.REVIEW_MERGED,
            {
                "issue_number": action.issue_number,
                "issue_key": action.issue_key or str(action.issue_number),
                "pr_number": action.pr_number,
                "pr_url": action.pr_url,
                "source": action.source,
            },
        ))

    # Every successful awaiting-merge mutation is terminal (merged or closed).
    terminate_issue_runtime(action.issue_number, "issue-completed")
    return ActionResult.ok(
        action,
        issue_number=action.issue_number,
        pr_number=action.pr_number,
        previous_status=outcome.previous_status,
        status=outcome.status,
    )


def _shipped_fix_recorder(
    status: AwaitingMergeTerminalStatus,
    authority: TriageAuthorityStore | None,
) -> Callable[[SessionHistoryEntry], None] | None:
    if status != "merged":
        return None
    return partial(_record_area_tagged_shipped_fix, authority)


def _record_area_tagged_shipped_fix(
    authority: TriageAuthorityStore | None,
    entry: SessionHistoryEntry,
) -> None:
    area = triage_area_from_labels(entry.issue_labels)
    if not area:
        return
    if authority is None:
        raise RuntimeError(
            "Triage authority store is required to record an area-tagged"
            f" shipped fix for issue #{entry.issue_number}"
        )
    if entry.pr_url is None:
        raise RuntimeError(
            f"Area-tagged shipped fix for issue #{entry.issue_number} has no PR URL"
        )
    authority.record_shipped_fix(
        issue_number=entry.issue_number,
        title=entry.title,
        pr_url=entry.pr_url,
        area=area,
    )


def _log_noop(
    action: ReconcileHistoryEntryAction,
    reason: str,
    current_status: str | None,
) -> None:
    if reason == "missing":
        logger.warning(
            "Awaiting-merge history reconciliation missing entry: "
            "issue=%d pr=%d pr_url=%s status=%s",
            action.issue_number,
            action.pr_number,
            action.pr_url,
            action.status,
        )
        return
    logger.info(
        "Awaiting-merge history reconciliation no-op: "
        "issue=%d pr=%d current_status=%s status=%s",
        action.issue_number,
        action.pr_number,
        current_status,
        action.status,
    )
