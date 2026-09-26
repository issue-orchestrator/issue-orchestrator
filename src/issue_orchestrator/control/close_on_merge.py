"""Close-on-merge fallback and awaiting-merge fact builders.

GitHub's closing-keyword parse was the ONLY close-on-merge mechanism. It is
word-boundary sensitive and easily defeated — a hand-authored recovery PR whose
body contained literal ``\\n`` escapes ("...\\n\\nCloses #45.") left the issue
open after its PR merged. The awaiting-merge reconciler shed the stale labels
and terminalized history but issued no close, so the first planning pass after
a restart relaunched a coding session on the already-merged issue (porchpin
case file #81, ``merged-unclosed-issue-relaunched-after-restart``).

The merged-terminal discovery therefore reads the issue's live state once, at
the terminal transition (not per-tick — the #6600 rollup-noise removal is
untouched), and carries ``issue_open`` on the reconciliation fact so the
Planner can order a close on the terminal-recovery owner command. Only a
MERGED PR earns the fallback: closed-unmerged PRs keep their drift-path
behavior, and intentionally reopened issues (porchpin case file #59) are never
touched — their history entries are already terminal and cannot re-fire.

A merged PR that declares partial delivery (``Refs #N``, from ``coding-done
completed --partial``) is the one merged PR that must NOT close its issue: the
issue's acceptance spans several PRs and this was one of them (#7288).
:func:`merged_pr_reconciliation` owns that rule for every discovery path, and
the fact it builds carries ``partial_pr`` so the apply side leaves the issue
open and schedulable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, cast

from ..domain.models import (
    AwaitingMergeReconciliationSource,
    AwaitingMergeTerminalStatus,
    DiscoveredAwaitingMergeReconciliation,
)
from ..domain.pr_issue_reference import declares_partial_delivery
from ..ports.repository_host import RepositoryHostError
from .actions import ActionResult, CloseIssueAction
from .awaiting_merge_post_publish_policy import normalized_state
from .queue_cache import record_issue_refreshes

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, SessionHistoryEntry
    from ..ports.issue import Issue
    from ..ports.pull_request_tracker import PRInfo
    from ..ports.repository_host import RepositoryHost
    from .actions import RecoverTerminalIssueAction

logger = logging.getLogger(__name__)


def close_on_merge_evidence(
    *,
    get_issue: "Callable[[int], Issue | None]",
    closed_on_or_after: "Callable[[int, str], bool]",
    issue_number: int,
    merged_at: str | None,
    on_issue_read: Callable[[], None] | None = None,
) -> bool | None:
    """Whether the issue behind a merged PR needs the fallback close — the
    single owner of the destructive precondition.

    True only on positive evidence of the failure this fallback exists for:
    the issue is open AND no ``closed`` event exists at/after the PR's
    ``merged_at`` — i.e. GitHub's auto-close never fired for this merge. An
    open issue that HAS a close event since the merge was auto-closed and then
    deliberately reopened; it is never re-closed. A missing ``merged_at``
    means no evidence either way — never infer a destructive close from
    ``state == open`` alone.

    Called from BOTH phases: discovery (to plan the close attempt) and the
    apply-time owner command, which revalidates immediately before the
    destructive write — the planner's bit is advisory and can go stale in the
    discovery→apply gap (a human closing and reopening in between).

    Returns None when the evidence cannot be read (transient repository-host
    error) so the caller can retry without mutating: fail-open recreates the
    relaunch bug, and raising aborts the caller's tick. An issue the host
    reports as missing is treated as not-open — there is nothing to close.
    """
    try:
        issue = get_issue(issue_number)
    except RepositoryHostError:
        logger.warning(
            "Unable to check issue state for merged PR close fallback: "
            "issue=#%d; retrying without mutation",
            issue_number,
        )
        return None
    if issue is None:
        return False
    if on_issue_read is not None:
        on_issue_read()
    if normalized_state(issue.state) == "closed":
        return False
    if not merged_at:
        logger.warning(
            "Merged PR for issue #%d carries no merged_at; skipping close-on-"
            "merge fallback — open state alone is not evidence of a failed "
            "auto-close",
            issue_number,
        )
        return False
    try:
        auto_close_fired = closed_on_or_after(issue_number, merged_at)
    except RepositoryHostError:
        logger.warning(
            "Unable to read close events for merged PR close fallback: "
            "issue=#%d; retrying without mutation",
            issue_number,
        )
        return None
    if auto_close_fired:
        logger.info(
            "Issue #%d was closed at/after its PR merge and deliberately "
            "reopened; close-on-merge fallback will not re-close it",
            issue_number,
        )
        return False
    return True


def should_close_merged_issue(
    *,
    get_issue: "Callable[[int], Issue | None]",
    closed_on_or_after: "Callable[[int, str], bool]",
    state: "OrchestratorState",
    entry: "SessionHistoryEntry",
    merged_at: str | None,
    now: float,
) -> bool | None:
    """Discovery-phase wrapper: the shared evidence rule plus queue-cache
    freshness bookkeeping for the issue read."""
    return close_on_merge_evidence(
        get_issue=get_issue,
        closed_on_or_after=closed_on_or_after,
        issue_number=entry.issue_number,
        merged_at=merged_at,
        on_issue_read=lambda: record_issue_refreshes(
            state, {entry.issue_number}, now,
        ),
    )


def merged_pr_reconciliation(
    *,
    get_issue: "Callable[[int], Issue | None]",
    closed_on_or_after: "Callable[[int, str], bool]",
    state: "OrchestratorState",
    entry: "SessionHistoryEntry",
    pr: "PRInfo",
    now: float,
) -> DiscoveredAwaitingMergeReconciliation | None:
    """The terminal fact for a history entry whose PR merged.

    A partial PR leaves its issue open by design, so it gets no close check
    and no close. Every other merged PR gets the close-on-merge evidence
    check. Returns None when that evidence cannot be read; the caller leaves
    the entry reconcilable.
    """
    partial = declares_partial_delivery(pr.body, entry.issue_number)
    issue_open = False
    if not partial:
        close_check = should_close_merged_issue(
            get_issue=get_issue, closed_on_or_after=closed_on_or_after,
            state=state, entry=entry, merged_at=pr.merged_at, now=now,
        )
        if close_check is None:
            return None
        issue_open = close_check
    return reconciliation_fact(
        entry=entry,
        pr_number=pr.number,
        status="merged",
        reason=pr_terminal_reason("merged", partial_pr=partial),
        source="pull_request",
        issue_open=issue_open,
        merged_at=pr.merged_at,
        partial_pr=partial,
    )


def partial_merge_label_recovery(
    issue_number: int, pr: "PRInfo"
) -> DiscoveredAwaitingMergeReconciliation:
    """Terminal fact for an untracked pr-pending issue whose latest PR is a
    merged partial one. The label drift scan finds these (for example after
    a restart). No PR is open, so ``pr-pending`` is stale. The issue has
    remaining work, so it is released, not closed and not flagged
    ``blocked:pr-closed``.
    """
    return DiscoveredAwaitingMergeReconciliation(
        issue_number=issue_number,
        pr_number=pr.number,
        pr_url=pr.url,
        status="merged",
        status_reason=pr_terminal_reason("merged", partial_pr=True),
        source="pull_request",
        merged_at=pr.merged_at,
        partial_pr=True,
    )


def run_close_on_merge_fallback(
    *,
    repository_host: object,
    action: "RecoverTerminalIssueAction",
    close: "Callable[[CloseIssueAction], ActionResult]",
) -> tuple[bool, str | None]:
    """Apply-time owner of the fallback close. Returns (close_applied, error).

    Revalidates the destructive precondition against live state immediately
    before the write — the planner's ``close_issue`` bit is advisory and can
    go stale in the discovery→apply gap (a human closing, or closing and
    deliberately reopening, in between). Only the shared evidence rule (issue
    open AND no close event since ``merged_at``) authorizes the close.

    Ordering: the close is the FIRST mutation of terminal recovery, before
    the label shed — a closed issue can never re-enter the work queue, so a
    later shed or history failure is safe and retryable. The reverse order
    would open a window (queue-gating labels shed, close failed, process
    restarted) where the first planning pass relaunches the issue through
    exactly the hole this fallback closes. A close that succeeds while
    history finalization fails reconciles terminal-via-issue-closure on the
    next pass — idempotent.

    A non-None error means fail WITHOUT any further mutation (no shed, no
    history): unreadable evidence or a failed close both leave the entry
    reconcilable for retry.
    """
    host = cast("RepositoryHost", repository_host)
    evidence = close_on_merge_evidence(
        get_issue=host.get_issue,
        closed_on_or_after=host.issue_closed_on_or_after,
        issue_number=action.issue_number,
        merged_at=action.merged_at or None,
    )
    if evidence is None:
        return False, (
            "close-on-merge revalidation unreadable; awaiting-merge history "
            "left reconcilable for retry"
        )
    if not evidence:
        # Already closed, or deliberately reopened after an auto-close — no
        # destructive write; the caller proceeds with shed + history.
        return False, None
    result = close(CloseIssueAction(
        issue_number=action.issue_number,
        comment=close_on_merge_comment(action.pr_url, action.pr_number),
        reason=action.status_reason or action.reason,
    ))
    if not result.success:
        return False, (
            "close-on-merge fallback failed; awaiting-merge history left "
            f"reconcilable for retry: {result.error}"
        )
    return True, None


def close_on_merge_comment(pr_url: str, pr_number: int) -> str:
    """Explanatory comment posted when the fallback closes an issue."""
    return (
        f"Closing: {pr_url or f'PR #{pr_number}'} merged this issue's work, "
        "but the PR registered no closing reference, so GitHub did not "
        "auto-close the issue. The orchestrator closed it during "
        "awaiting-merge reconciliation. If this issue was intentionally left "
        "open for remaining scope, reopen it."
    )


def reconciliation_fact(
    *,
    entry: "SessionHistoryEntry",
    pr_number: int,
    status: AwaitingMergeTerminalStatus,
    reason: str,
    source: AwaitingMergeReconciliationSource,
    issue_open: bool = False,
    merged_at: str | None = None,
    partial_pr: bool = False,
) -> DiscoveredAwaitingMergeReconciliation:
    return DiscoveredAwaitingMergeReconciliation(
        issue_number=entry.issue_number,
        pr_number=pr_number,
        pr_url=entry.pr_url or "",
        status=status,
        status_reason=reason,
        source=source,
        issue_open=issue_open,
        merged_at=merged_at,
        partial_pr=partial_pr,
    )


def pr_terminal_reason(
    status: AwaitingMergeTerminalStatus, *, partial_pr: bool = False
) -> str:
    if status == "merged" and partial_pr:
        return "Partial PR merged; issue stays open for its remaining work"
    if status == "merged":
        return "PR merged; awaiting merge reconciled"
    return "PR closed; awaiting merge reconciled"
