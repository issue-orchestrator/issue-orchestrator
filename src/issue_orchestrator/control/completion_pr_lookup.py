"""Resolve the pull request a completed session produced.

Extracted from ``CompletionHandler`` (#6858 rework 1). "Which PR did this
session produce?" is a distinct step: it talks to exactly one collaborator (the
repository host) and touches none of the handler's state-machine, trace-event,
history or cleanup concerns. Giving it its own owner keeps
``completion_handler.py`` inside its line budget and makes the lookup — with its
three distinct sources (hint, branch, review fallback) — directly testable
instead of reachable only through a full completion.

The result is a frozen value rather than the bare ``(url, number, prs)`` tuple
the handler used to unpack, so callers name what they read.
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..domain.completion_processing import CompletionPublication
from ..domain.models import DiscoveredReview, Session, SessionStatus
from ..domain.session_key import TaskKind
from ..infra.logging_config import log_context
from ..ports import RepositoryHost

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedCompletionPr:
    """The pull request a completed session produced, if there is one.

    ``pull_requests`` carries the full fetched records when the lookup had them,
    because the caller emits a PR view from the first one; ``url``/``number``
    alone mean the PR is known by reference but was not fetched.
    """

    url: Optional[str] = None
    number: Optional[int] = None
    pull_requests: Optional[list[Any]] = None
    publication: CompletionPublication | None = None


    def review_for(self, session: Session) -> DiscoveredReview:
        """Carry the published PR branch across the completion/queue boundary."""
        if self.publication is None or self.number is None or self.url != self.publication.url:
            raise ValueError("review routing requires the publication identity")
        return DiscoveredReview(session.issue.number, self.number, self.publication.url, self.publication.branch,
            agent_label=session.agent_label, issue_key=session.issue.key.stable_id())


#: The answer whenever a session produced no PR — not completed, a
#: retrospective review, or nothing found by any source.
NO_COMPLETION_PR = ResolvedCompletionPr()


class CompletionPrLookup:
    """Answers "which PR did this session produce?" against the repository host."""

    def __init__(self, repository_host: RepositoryHost) -> None:
        self._repository_host = repository_host

    def for_session(
        self,
        session: Session,
        status: SessionStatus,
        *,
        pr_url_hint: Optional[str] = None,
        publication_hint: CompletionPublication | None = None,
    ) -> ResolvedCompletionPr:
        """Resolve the PR for a completed session.

        ``pr_url_hint`` short-circuits the branch lookup (dry-run mode).
        """
        if status != SessionStatus.COMPLETED:
            return NO_COMPLETION_PR

        if session.key.task == TaskKind.RETROSPECTIVE_REVIEW:
            return NO_COMPLETION_PR

        if pr_url_hint:
            return self._from_hint(session, pr_url_hint, publication_hint)

        return self._from_branch_or_review_fallback(session)

    def _from_hint(self, session: Session, pr_url_hint: str,
                   publication: CompletionPublication | None) -> ResolvedCompletionPr:
        if publication is not None and publication.url != pr_url_hint:
            raise ValueError("publication hint differs from completion URL")
        pr_url = pr_url_hint
        pr_number: Optional[int] = None
        prs: Optional[list[Any]] = None

        match = re.search(r"/pull/(\d+)", pr_url)
        if match:
            pr_number = int(match.group(1))
            try:
                pr_info = self._repository_host.get_pr(pr_number)
            except Exception as e:
                logger.warning("Failed to fetch PR %s for PR hint: %s", pr_number, e)
            else:
                if pr_info:
                    prs = [pr_info]
                    publication = CompletionPublication(pr_info.url, pr_info.branch)

        logger.info(
            "[PR_HINT] Using PR from completion processor: %s (number=%s)",
            pr_url,
            pr_number,
            extra=log_context(issue_key=session.key.issue.stable_id(), session_id=session.terminal_id),
        )
        return ResolvedCompletionPr(url=pr_url, number=pr_number, pull_requests=prs, publication=publication)

    def _from_branch_or_review_fallback(self, session: Session) -> ResolvedCompletionPr:
        logger.debug("[ADAPTER] Using GitHubAdapter for get_prs_for_branch")
        start = time.monotonic()
        pr_infos = self._repository_host.get_prs_for_branch(session.branch_name)
        duration = time.monotonic() - start
        logger.info(
            "Fetched PRs for branch in %.2fs: branch=%s count=%d",
            duration,
            session.branch_name,
            len(pr_infos),
            extra=log_context(issue_key=session.key.issue.stable_id(), session_id=session.terminal_id),
        )
        if pr_infos:
            return ResolvedCompletionPr(
                url=pr_infos[0].url,
                number=pr_infos[0].number,
                pull_requests=list(pr_infos),
                publication=CompletionPublication(pr_infos[0].url, pr_infos[0].branch),
            )

        if session.pr_number is None:
            return NO_COMPLETION_PR

        try:
            review_pr = self._repository_host.get_pr(session.pr_number)
        except Exception as e:
            logger.warning(
                "Failed to fetch PR %s for review session fallback: %s",
                session.pr_number,
                e,
            )
            return NO_COMPLETION_PR

        if review_pr:
            return ResolvedCompletionPr(
                url=review_pr.url, number=review_pr.number, pull_requests=[review_pr],
                publication=CompletionPublication(review_pr.url, review_pr.branch)
            )

        return NO_COMPLETION_PR
