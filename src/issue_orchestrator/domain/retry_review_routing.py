"""Original session policy carried through publication retries."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryReviewRouting:
    branch_name: str
    skip_review: bool
    review_exchange_completed: bool
    review_exchange_halted: bool
