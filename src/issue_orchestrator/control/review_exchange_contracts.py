"""Shared review-exchange lifecycle contracts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class ReviewExchangeCancellationResult(Protocol):
    """Structured result returned by issue-scoped review-exchange cancellation."""

    @property
    def cancelled_job_ids(self) -> tuple[str, ...]:
        ...


ReviewExchangeCanceller = Callable[[int, str], ReviewExchangeCancellationResult]
