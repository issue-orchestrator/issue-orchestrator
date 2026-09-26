"""Classify awaiting-merge label drift from an issue's associated PR set."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.models import (
    DiscoveredAwaitingMergeDrift,
    DiscoveredAwaitingMergeReconciliation,
)
from ..domain.pr_issue_reference import declares_partial_delivery
from .close_on_merge import partial_merge_label_recovery

if TYPE_CHECKING:
    from ..ports.pull_request_tracker import PRInfo


@dataclass(frozen=True)
class PrSetDriftClassification:
    """Whether an issue's PR set indicates ``blocked:pr-closed`` drift.

    ``drifting`` is true when the issue should be flagged. ``pr`` is the PR the
    drift keys on, or ``None`` for the "no associated PR" case.
    """

    drifting: bool
    pr: PRInfo | None = None
    # The latest terminal PR merged as a partial delivery ("Refs #N", #7288).
    # No PR is open, so ``pr-pending`` is stale, but the issue has work left:
    # the issue is released for its next slice, not flagged as drift.
    partial_merge: PRInfo | None = None


def classify_pr_set_drift(
    prs: list[PRInfo], *, issue_number: int
) -> PrSetDriftClassification:
    """Own the ``blocked:pr-closed`` precedence policy for a PR set.

    Policy:
    - Any open PR means the issue is still legitimately awaiting a merge.
    - No PRs means drift with no PR reference ("PR missing").
    - Otherwise, the latest terminal PR decides: merged suppresses drift;
      closed-unmerged produces drift keyed on that latest PR. A merged PR
      that declared partial delivery of ``issue_number`` suppresses drift and
      reports ``partial_merge`` instead: GitHub will not close that issue.
    """
    if any(_normalized_state(pr.state) == "open" for pr in prs):
        return PrSetDriftClassification(drifting=False)
    if not prs:
        return PrSetDriftClassification(drifting=True)
    latest = max(prs, key=lambda item: item.number)
    if latest.is_closed_unmerged:
        return PrSetDriftClassification(drifting=True, pr=latest)
    if declares_partial_delivery(latest.body, issue_number):
        return PrSetDriftClassification(drifting=False, partial_merge=latest)
    return PrSetDriftClassification(drifting=False)


def _normalized_state(state: str | None) -> str:
    return (state or "").strip().lower()


def label_drift_finding(
    issue_number: int, prs: list[PRInfo]
) -> DiscoveredAwaitingMergeDrift | DiscoveredAwaitingMergeReconciliation | None:
    """The fact a pr-pending issue's PR set produces, if any.

    Closed-unmerged or missing PRs produce ``blocked:pr-closed`` drift. A
    latest merged partial PR produces a terminal recovery that sheds the stale
    ``pr-pending`` label and leaves the issue open for its next slice.
    """
    decision = classify_pr_set_drift(prs, issue_number=issue_number)
    if decision.partial_merge is not None:
        return partial_merge_label_recovery(issue_number, decision.partial_merge)
    if not decision.drifting:
        return None
    if decision.pr is None:
        return DiscoveredAwaitingMergeDrift(
            issue_number=issue_number,
            pr_number=0,
            pr_url="",
            status_reason="PR missing; issue remains open",
        )
    return DiscoveredAwaitingMergeDrift(
        issue_number=issue_number,
        pr_number=decision.pr.number,
        pr_url=decision.pr.url,
        status_reason="PR closed; issue remains open",
    )
