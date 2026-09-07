"""Retrieve and render bounded review feedback for escalation comments."""

import logging
from ..ports.repository_host import RepositoryHost

logger = logging.getLogger(__name__)


def latest_review_section(
    repository_host: RepositoryHost | None, pr_number: int, provided_body: str | None
) -> str:
    """Build the latest review section for escalation comments.

    Returns formatted markdown section or empty string.
    """
    review_body = provided_body
    if not review_body and repository_host:
        try:
            reviews = repository_host.get_pr_reviews(pr_number)
            for review in reversed(reviews):
                if review.get("state") == "CHANGES_REQUESTED" and review.get("body"):
                    review_body = review.get("body", "")
                    break
        except Exception as e:
            logger.debug("Failed to fetch PR reviews: %s", e)

    if not review_body:
        return ""

    if len(review_body) > 1000:
        review_body = review_body[:1000] + "..."
    return f"""
### Latest Review Feedback

<details>
<summary>Reviewer's comments (click to expand)</summary>

{review_body}

</details>
"""
