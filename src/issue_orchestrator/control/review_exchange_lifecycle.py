"""Issue-scoped review-exchange lifecycle helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .completion_review_exchange import is_review_exchange_job_for_issue

if TYPE_CHECKING:
    from ..ports.persistent_exchange_pair_registry import (
        PersistentExchangePairRegistry,
    )
    from .background_job_supervisor import BackgroundJobSupervisor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewExchangeCancellation:
    """Result of cancelling review-exchange work for one issue."""

    issue_number: int
    cancelled_job_ids: tuple[str, ...]


def cancel_issue_review_exchange(
    *,
    issue_number: int,
    reason: str,
    pair_registry: "PersistentExchangePairRegistry | None",
    job_supervisor: "BackgroundJobSupervisor | None",
) -> ReviewExchangeCancellation:
    """Terminate persistent pair resources and stop supervised polling.

    Review exchange work has two owners: the pair registry owns the persistent
    coder/reviewer processes, and the background supervisor owns the async job
    status observed by the main tick. Operator cancellation must touch both or
    the visible issue session can stop while a hidden exchange continues to
    report "still running".
    """
    if pair_registry is not None:
        pair_registry.release(issue_number, reason=reason)

    cancelled: tuple[str, ...] = ()
    if job_supervisor is not None:
        cancelled = tuple(
            job_supervisor.cancel_matching(
                lambda job_id: is_review_exchange_job_for_issue(job_id, issue_number),
                reason=reason,
            )
        )
    if pair_registry is not None or cancelled:
        logger.info(
            "[REVIEW_EXCHANGE] cancelled issue=%d reason=%s jobs=%s",
            issue_number,
            reason,
            ",".join(cancelled) if cancelled else "none",
        )
    return ReviewExchangeCancellation(
        issue_number=issue_number,
        cancelled_job_ids=cancelled,
    )
