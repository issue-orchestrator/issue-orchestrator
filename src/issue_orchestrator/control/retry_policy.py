"""Shared retry/unblock policy for UI-driven actions."""

from __future__ import annotations

from typing import Sequence

from .label_manager import LabelManager
from .published_review_custody import IssuePullRequestReader, open_pull_requests


def labels_to_remove_for_retry(
    labels: Sequence[str], lm: LabelManager, *, has_open_pr: bool
) -> list[str]:
    """Return labels that must be removed before an issue can be retried.

    Retry should clear:
    - all blocking labels
    - tech_lead needs-human provenance paired with a cleared needs-human label
    - pr-pending (scheduler-gating lifecycle label), unless the issue still has
      an open PR (#7293). pr-pending is what keeps the scheduler from starting
      a second attempt beside that PR; for such an issue Retry means "release
      its review", and stripping the label would launch a duplicate coder
      while the PR waits.
    """
    labels_to_remove = set(lm.get_blocking(labels)) | (
        {lm.tech_lead_needs_human} & set(labels)
    )
    if lm.is_pr_pending(labels) and not has_open_pr:
        labels_to_remove.add(lm.pr_pending)
    return sorted(labels_to_remove)


def retry_label_removals(
    issue_number: int,
    labels: Sequence[str],
    lm: LabelManager,
    pull_requests: IssuePullRequestReader,
) -> list[str]:
    """The one owner of WHICH labels a retry clears, observation included.

    The open-PR fact is read only when it can change the answer (pr-pending is
    present), so a retry on an ordinary blocked issue costs no PR read.
    """
    has_open_pr = lm.is_pr_pending(labels) and bool(
        open_pull_requests(pull_requests, issue_number)
    )
    return labels_to_remove_for_retry(labels, lm, has_open_pr=has_open_pr)
