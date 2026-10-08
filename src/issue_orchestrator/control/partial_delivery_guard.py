"""The one guard that keeps a partial delivery from closing its issue (#7288).

A PR that delivers part of an issue says ``Refs #N``. GitHub still closes the
issue on merge if the PR body, an existing PR's body, or any commit on the
branch names it in a closing keyword. Those commits close it the moment they
reach the default branch, and a squash merge copies their messages. So this
guard runs BEFORE every branch write (the live push, its rebase retry, and
the preparation that precedes manual and retained publication), not after:
once a closing commit sits on an open partial PR, a later refusal cannot take
it back.

Delivery is partial when the completion claims it, when the issue's latest
merged PR only refs it and the completion does not declare it finished
(:meth:`PartialDeliveryGuard.delivery`, #8689), or when the branch's open PR
already refs the issue. The last case covers a rework that keeps an existing
partial PR's reference line without repeating ``--partial``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.issue_delivery import (
    DeliveryBasis,
    IssueDelivery,
    MergedPullRequest,
    delivery_from_history,
    stated_delivery,
)
from ..domain.pr_issue_reference import (
    declares_partial_delivery,
    honors_delivery_claim,
    issue_links,
    names_issue_in_closing_keyword,
)
from ..domain.host_rate_limit import HostRateLimit
from ..ports.pull_request_tracker import PRInfo
from ..ports.repository_host import host_rate_limit_of
from .publication_source_guards import PublicationSourceGuards


@dataclass(frozen=True)
class PartialDeliveryRefusal:
    """Why a branch write was refused, and whether a later attempt can pass.

    ``retryable`` is False when the words themselves close the issue - the
    agent's text or an existing PR's reference line must change first. It is
    True when the guard simply could not read what it needed (the branch's
    commit messages, or its open PR): that says nothing about the delivery,
    so the publication stays retryable, and ``host_rate_limit`` names the
    reset when GitHub refused the read (#7297).
    """

    reason: str
    retryable: bool = False
    host_rate_limit: HostRateLimit | None = None


class DeliveryPullRequests(Protocol):
    """The PR reads the guard needs: the branch's open PRs, and the issue's
    merged PRs with their bodies."""

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]: ...
    def merged_pr_history(self, issue_number: int) -> tuple[MergedPullRequest, ...]: ...


class PartialDeliveryGuard:
    """Refuses a branch write that would close an issue delivered in part."""

    def __init__(
        self,
        *,
        source: PublicationSourceGuards,
        prs: DeliveryPullRequests,
        repo_slug: Callable[[], str],
    ) -> None:
        self._source = source
        self._prs = prs
        self._repo_slug = repo_slug

    def delivery(
        self, issue_number: int, *, partial_pr: bool, finishes_issue: bool,
    ) -> IssueDelivery | PartialDeliveryRefusal:
        """The delivery a publication makes for ``issue_number`` (#8689).

        A completion's own claim decides. One that states nothing is judged
        against the issue's merged PRs, so a receipt that forgot ``--partial``
        cannot close an issue that earlier PRs delivered in part. A failed
        history read is a retryable refusal: unread, not whole.
        """
        stated = stated_delivery(partial_pr=partial_pr, finishes_issue=finishes_issue)
        if stated is not None:
            return stated
        try:
            history = self._prs.merged_pr_history(issue_number)
            if not history:
                # Nothing merged: no slug is needed to know the issue is whole.
                return IssueDelivery(DeliveryBasis.WHOLE)
            return delivery_from_history(issue_number, history, repo_slug=self._repo_slug())
        except Exception as exc:
            return PartialDeliveryRefusal(
                f"could not read the merged PRs of #{issue_number} to tell whether it "
                f"was already delivered in part: {exc}",
                retryable=True,
                host_rate_limit=host_rate_limit_of(exc),
            )

    def refusal(
        self,
        worktree: Path,
        *,
        issue_number: int,
        branch: str,
        delivery: IssueDelivery,
        claim_body: str,
    ) -> PartialDeliveryRefusal | None:
        """Why writing ``branch`` would close ``issue_number`` despite a partial
        delivery, or None when the write is safe.

        ``delivery`` comes from :meth:`delivery`; ``claim_body`` is the PR body
        this publication would carry. The repository slug and the open-PR
        read are only needed when something could break a partial delivery:
        a partial one, or a commit that closes ``#issue_number`` in some
        repository.
        """
        stated, finishing = delivery.partial, delivery.finishes
        messages, unreadable = self._source.branch_commit_messages(worktree)
        if unreadable is not None:
            # Unread, not unsafe: the same retryable refusal as a failed PR read.
            return PartialDeliveryRefusal(
                f"{unreadable}; refusing to publish without the partial-delivery check",
                retryable=True,
            )
        may_close = [m for m in messages if _closes_some_repos_issue(m, issue_number)]
        if not stated and not finishing and not may_close:
            return None
        slug = self._repo_slug()
        if stated and not declares_partial_delivery(claim_body, issue_number, repo_slug=slug):
            return PartialDeliveryRefusal(
                f"{_partial_because(delivery, issue_number)}, but its "
                f"implementation or problems text closes it by keyword; reword it to "
                f"'Refs #{issue_number}' or {_publish_whole_hint(delivery)}"
            )
        try:
            open_prs = [
                pr for pr in self._prs.get_prs_for_branch(branch, state="open")
                if pr.branch == branch
            ]
        except Exception as exc:
            return PartialDeliveryRefusal(
                f"could not read the open PR of branch {branch!r} for the "
                f"partial-delivery check: {exc}",
                retryable=True,
                host_rate_limit=host_rate_limit_of(exc),
            )
        # Reuse keeps an open PR's body, so that body must carry this
        # delivery's claim: partial must not close, finishing must close.
        mismatched = next(
            (pr for pr in open_prs if not honors_delivery_claim(
                pr.body, issue_number, partial=stated, finishes=finishing, repo_slug=slug)),
            None,
        )
        if mismatched is not None and finishing:
            return PartialDeliveryRefusal(
                f"completion declared that it finishes #{issue_number}, but existing PR "
                f"#{mismatched.number} does not close it, so merging it would leave "
                f"#{issue_number} open; change its reference line to 'Closes #{issue_number}' "
                f"or complete without --finishes-issue"
            )
        if mismatched is not None:
            return PartialDeliveryRefusal(
                f"{_partial_because(delivery, issue_number)}, but "
                f"existing PR #{mismatched.number} closes it on merge; change its "
                f"reference line to 'Refs #{issue_number}' or {_publish_whole_hint(delivery)}"
            )
        partial = stated or any(
            declares_partial_delivery(pr.body, issue_number, repo_slug=slug) for pr in open_prs
        )
        closing = [
            m.splitlines()[0] for m in may_close
            if names_issue_in_closing_keyword(m, issue_number, repo_slug=slug)
        ]
        if not partial or not closing:
            return None
        because, way_out = (
            (_partial_because(delivery, issue_number), f", or {_publish_whole_hint(delivery)}")
            if stated else (f"#{issue_number} is being delivered in part (its open PR refs it)", "")
        )
        return PartialDeliveryRefusal(
            f"{because}, but "
            f"{len(closing)} commit(s) on {branch!r} close it by keyword (first: "
            f"{closing[0]!r}); GitHub would close #{issue_number} on merge. Reword them "
            f"to 'Refs #{issue_number}' before publishing{way_out}"
        )


def _closes_some_repos_issue(message: str, issue_number: int) -> bool:
    """A cheap, repository-agnostic pre-filter: only a message that closes
    some repository's ``#issue_number`` can need the scoped check."""
    return any(link.closes and link.number == issue_number for link in issue_links(message))


def _partial_because(delivery: IssueDelivery, issue_number: int) -> str:
    """The partial delivery a refusal protects, in the words that explain it."""
    if delivery.inferred:
        return (
            f"#{issue_number} was already delivered in part (merged PR "
            f"#{delivery.evidence_pr} refs it), so this completion publishes it as partial"
        )
    return f"completion declared partial delivery of #{issue_number}"


def _publish_whole_hint(delivery: IssueDelivery) -> str:
    """How the agent publishes a whole delivery instead."""
    if delivery.inferred:
        return "complete with --finishes-issue if this PR finishes it"
    return "publish without --partial"
