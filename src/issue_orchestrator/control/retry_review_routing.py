"""One review candidate policy shared by manual and staged publication."""

from ..domain.models import DiscoveredReview
from ..domain.retry_review_routing import RetryReviewRouting
from .review_routing import should_queue_pr_review


class RetryReviewPolicy:
    def __init__(self, *, code_review_agent_configured: bool) -> None:
        self._code_review_agent_configured = code_review_agent_configured

    def candidate(
        self,
        *,
        issue_number: int,
        pr_url: str | None,
        pr_number: int | None,
        agent_label: str | None,
        routing: RetryReviewRouting,
    ) -> DiscoveredReview | None:
        """Build the review-discovery candidate for a still-unreviewed PR (pure).

        A publish failure is only ever recorded for a work session (review
        sessions do not push / create PRs), so this PR always came from a work
        session. When code review still applies, return the same
        ``DiscoveredReview`` fact live completion uses so the planner owns
        pr-pending + the review queue (and the dry-run / already-queued gates).
        Returns ``None`` when no review is needed. Performs no mutation.
        """
        if pr_url is None or pr_number is None:
            return None
        if not should_queue_pr_review(
            has_pr=True,
            code_review_agent_configured=self._code_review_agent_configured,
            skip_review=routing.skip_review,
            is_review_session=False,
            review_exchange_completed=routing.review_exchange_completed,
            review_exchange_halted=routing.review_exchange_halted,
        ):
            return None
        return DiscoveredReview(
            issue_number,
            pr_number,
            pr_url,
            routing.branch_name,
            agent_label=agent_label,
        )
