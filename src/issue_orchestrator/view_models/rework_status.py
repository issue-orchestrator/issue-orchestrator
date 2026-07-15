"""Single owner for the "is this issue queued for rework, and why" question.

A PR that needs rework (a reviewer requested changes, or a post-publish
merge/validation problem) is queued against its *source issue* via
``state.pending_reworks`` (already queued) or ``state.discovered_reworks``
(scanned this tick, about to be queued). Before the rework session launches,
the dashboard card and the issue-detail drawer both need to explain that the
issue is *queued for rework* — including the PR number, the rework cycle, and a
short reason (e.g. "Merge conflict against base branch").

This module is the one place that reads those state collections and derives the
user-facing reason, so the card projection and the detail projection cannot
drift apart (see the cross-cutting policy heuristic in the repo guide).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..control.awaiting_merge_post_publish_policy import POST_PUBLISH_VALIDATION_SOURCE

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState


# Source tag applied by the reviewer-driven rework flow.
REVIEW_LABEL_SOURCE = "review_label"


@dataclass(frozen=True)
class QueuedReworkStatus:
    """Why an issue is queued for rework, ready for user-facing display."""

    issue_number: int
    pr_number: int | None
    rework_cycle: int
    source: str
    reason: str

    @property
    def summary(self) -> str:
        """One-line explanation used by both the card and the detail drawer."""
        return format_queued_rework_summary(
            self.pr_number, self.rework_cycle, self.reason
        )


def format_queued_rework_summary(
    pr_number: int | None, rework_cycle: int, reason: str
) -> str:
    """Canonical "Queued for rework …" line shared by the card and the drawer.

    Both the dashboard card and the issue-detail drawer render this exact
    format so the two surfaces cannot describe the same queued rework
    differently.
    """
    cycle = rework_cycle if rework_cycle > 0 else 1
    pr = f" — PR #{pr_number}" if pr_number else ""
    return f"Queued for rework{pr} (cycle {cycle}): {reason}"


def resolve_queued_rework(
    state: "OrchestratorState", issue_number: int
) -> QueuedReworkStatus | None:
    """Return the queued-rework status for ``issue_number``, if any.

    ``pending_reworks`` (already queued by the planner) wins over
    ``discovered_reworks`` (scanned but not yet queued) so an issue mid-tick
    reports its authoritative queued cycle rather than the raw scan.
    """
    for rework in state.pending_reworks:
        if rework.resolve_issue_number() == issue_number:
            return _status_from(
                issue_number,
                rework.pr_number,
                rework.rework_cycle,
                rework.source,
                rework.feedback,
            )
    for discovered in state.discovered_reworks:
        if discovered.issue_number == issue_number:
            return _status_from(
                issue_number,
                discovered.pr_number,
                discovered.rework_cycle,
                discovered.source,
                discovered.feedback,
            )
    return None


def queued_rework_issue_numbers(state: "OrchestratorState") -> frozenset[int]:
    """Return the set of issue numbers currently queued for rework.

    This is the lane-eligibility companion to :func:`resolve_queued_rework`:
    the card projection calls ``resolve_queued_rework`` per issue to build the
    "Queued for rework …" summary, while the lane projections need only the
    *set* of queued-rework issues to keep them owned by the Queued lane. Both
    read the same ``pending_reworks`` / ``discovered_reworks`` collections, so
    an issue queued for rework cannot be classified one way for the card and
    another way for lane ownership.

    Without this single owner, a queued-rework issue that also has a stale
    completed history row (with a PR) would leak into Awaiting Merge and lane
    precedence would drop it from Queued.
    """
    numbers: set[int] = set()
    for rework in state.pending_reworks:
        number = rework.resolve_issue_number()
        if number is not None:
            numbers.add(number)
    for discovered in state.discovered_reworks:
        numbers.add(discovered.issue_number)
    return frozenset(numbers)


def _status_from(
    issue_number: int,
    pr_number: int | None,
    rework_cycle: int,
    source: str,
    feedback: str | None,
) -> QueuedReworkStatus:
    return QueuedReworkStatus(
        issue_number=issue_number,
        pr_number=pr_number if pr_number else None,
        rework_cycle=rework_cycle,
        source=source,
        reason=_reason_for(source, feedback),
    )


def _reason_for(source: str, feedback: str | None) -> str:
    """Derive a short, user-facing reason from the rework source and feedback."""
    if source == POST_PUBLISH_VALIDATION_SOURCE:
        return _first_meaningful_line(feedback) or "Post-publish validation failed"
    if source == REVIEW_LABEL_SOURCE:
        return "Reviewer requested changes"
    return _first_meaningful_line(feedback) or "Rework requested"


def _first_meaningful_line(feedback: str | None) -> str | None:
    """Return the first non-empty feedback line as a short reason.

    Post-publish feedback titles look like
    ``"Merge conflict against base branch (cycle handled ...):"`` — the
    parenthetical qualifier and trailing colon are trimmed so the reason reads
    as a plain phrase.
    """
    if not feedback:
        return None
    for raw in feedback.splitlines():
        line = raw.strip()
        if not line:
            continue
        head = line.split(" (", 1)[0].rstrip(":").strip()
        return head or line
    return None
