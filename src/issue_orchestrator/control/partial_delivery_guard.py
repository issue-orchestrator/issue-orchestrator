"""The one guard that keeps a partial delivery from closing its issue (#7288).

A PR that delivers part of an issue says ``Refs #N``. GitHub still closes the
issue on merge if the PR body, an existing PR's body, or any commit on the
branch names it in a closing keyword. Those commits close it the moment they
reach the default branch, and a squash merge copies their messages. So this
guard runs BEFORE every branch write (the live push, its rebase retry, and
the preparation that precedes manual and retained publication), not after:
once a closing commit sits on an open partial PR, a later refusal cannot take
it back.

Delivery is partial when the completion claims it, or when the branch's open
PR already refs the issue. The second case covers a rework that keeps an
existing partial PR's reference line without repeating ``--partial``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..domain.pr_issue_reference import (
    declares_partial_delivery,
    honors_partial_claim,
    issue_links,
    names_issue_in_closing_keyword,
)
from ..ports.pull_request_tracker import PRInfo
from .publication_source_guards import PublicationSourceGuards


class OpenBranchPullRequests(Protocol):
    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]: ...


class PartialDeliveryGuard:
    """Refuses a branch write that would close an issue delivered in part."""

    def __init__(
        self,
        *,
        source: PublicationSourceGuards,
        prs: OpenBranchPullRequests,
        repo_slug: Callable[[], str],
    ) -> None:
        self._source = source
        self._prs = prs
        self._repo_slug = repo_slug

    def refusal(
        self,
        worktree: Path,
        *,
        issue_number: int,
        branch: str,
        claimed: bool,
        claim_body: str,
    ) -> str | None:
        """Why writing ``branch`` would close ``issue_number`` despite a partial
        delivery, or None when the write is safe.

        ``claim_body`` is the PR body this completion would publish. The
        repository slug and the open-PR read are only needed when something
        could break a partial delivery: a partial claim, or a commit that
        closes ``#issue_number`` in some repository.
        """
        messages, unreadable = self._source.branch_commit_messages(worktree)
        if unreadable is not None:
            return f"{unreadable}; refusing to publish without the partial-delivery check"
        may_close = [m for m in messages if _closes_some_repos_issue(m, issue_number)]
        if not claimed and not may_close:
            return None
        slug = self._repo_slug()
        if claimed and not declares_partial_delivery(claim_body, issue_number, repo_slug=slug):
            return (
                f"completion declared partial delivery of #{issue_number}, but its "
                f"implementation or problems text closes it by keyword; reword it to "
                f"'Refs #{issue_number}' or publish without --partial"
            )
        try:
            open_prs = [
                pr for pr in self._prs.get_prs_for_branch(branch, state="open")
                if pr.branch == branch
            ]
        except Exception as exc:
            return (
                f"could not read the open PR of branch {branch!r} for the "
                f"partial-delivery check: {exc}"
            )
        closing_pr = next(
            (pr for pr in open_prs
             if not honors_partial_claim(pr.body, issue_number, partial=True, repo_slug=slug)),
            None,
        )
        if claimed and closing_pr is not None:
            return (
                f"completion declared partial delivery of #{issue_number}, but "
                f"existing PR #{closing_pr.number} closes it on merge; change its "
                f"reference line to 'Refs #{issue_number}' or publish without --partial"
            )
        partial = claimed or any(
            declares_partial_delivery(pr.body, issue_number, repo_slug=slug) for pr in open_prs
        )
        closing = [
            m.splitlines()[0] for m in may_close
            if names_issue_in_closing_keyword(m, issue_number, repo_slug=slug)
        ]
        if not partial or not closing:
            return None
        return (
            f"#{issue_number} is being delivered in part (Refs #{issue_number}), but "
            f"{len(closing)} commit(s) on {branch!r} close it by keyword (first: "
            f"{closing[0]!r}); GitHub would close #{issue_number} on merge. Reword them "
            f"to 'Refs #{issue_number}' before publishing"
        )


def _closes_some_repos_issue(message: str, issue_number: int) -> bool:
    """A cheap, repository-agnostic pre-filter: only a message that closes
    some repository's ``#issue_number`` can need the scoped check."""
    return any(link.closes and link.number == issue_number for link in issue_links(message))
