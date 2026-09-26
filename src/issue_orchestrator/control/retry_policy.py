"""Shared retry/unblock policy for UI-driven actions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Sequence

from .label_manager import LabelManager
from .review_scope import issues_with_open_prs

if TYPE_CHECKING:
    from ..ports.pull_request_tracker import PRInfo


class OpenPullRequestListing(Protocol):
    def list_open_prs_complete(self) -> list["PRInfo"]: ...


class OpenPullRequestIndex:
    """Which issues have an open PR - read at most ONCE per operator request.

    One complete, core-REST listing of the repository's open PRs (it raises
    rather than return a partial list), indexed by the issue each PR belongs
    to. Never a per-issue search: GitHub search is rate-limited far below what
    a bulk unblock of many issues would spend, and ``#N`` search terms also
    match PRs that merely mention the issue (#7293).
    """

    def __init__(self, listing: OpenPullRequestListing) -> None:
        self._listing = listing
        self._issues: frozenset[int] | None = None

    def has_open_pr(self, issue_number: int) -> bool:
        if self._issues is None:
            self._issues = issues_with_open_prs(self._listing.list_open_prs_complete())
        return issue_number in self._issues


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
    open_prs: OpenPullRequestIndex,
) -> list[str]:
    """The one owner of WHICH labels a retry clears, observation included.

    The open-PR fact is read only when it can change the answer (pr-pending is
    present), so a retry on an ordinary blocked issue costs no PR read.
    """
    has_open_pr = lm.is_pr_pending(labels) and open_prs.has_open_pr(issue_number)
    return labels_to_remove_for_retry(labels, lm, has_open_pr=has_open_pr)
