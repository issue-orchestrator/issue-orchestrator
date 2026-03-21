"""Shared validity checks for pending code reviews."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.issue import Issue
    from ..ports.pull_request_tracker import PRInfo
    from .label_manager import LabelManager


@dataclass(frozen=True)
class ReviewValidity:
    """Whether a queued/discovered review is still valid to process."""

    valid: bool
    reason: str
    issue_labels: tuple[str, ...] = ()
    pr_labels: tuple[str, ...] = ()
    blocking_labels: tuple[str, ...] = ()


def evaluate_review_validity(
    *,
    config: "Config",
    label_manager: "LabelManager",
    issue: "Issue | None",
    pr: "PRInfo | None" = None,
    review_label_confirmed: bool = False,
) -> ReviewValidity:
    """Return whether a review is still valid for queue/launch processing."""
    issue_labels = tuple(issue.labels) if issue is not None else ()
    pr_labels = tuple(pr.labels) if pr is not None else ()

    if pr is None and issue is None:
        return ReviewValidity(
            valid=True,
            reason="ok",
        )

    if pr is not None:
        if pr.state.lower() != "open":
            return ReviewValidity(
                valid=False,
                reason="pr_not_open",
                issue_labels=issue_labels,
                pr_labels=pr_labels,
            )

        review_label_missing = (
            config.code_review_label
            and not review_label_confirmed
            and config.code_review_label not in pr.labels
        )
        if review_label_missing:
            return ReviewValidity(
                valid=False,
                reason="review_label_missing",
                issue_labels=issue_labels,
                pr_labels=pr_labels,
            )

        pr_blocking = tuple(label_manager.get_blocking(pr.labels))
        if pr_blocking:
            return ReviewValidity(
                valid=False,
                reason="pr_blocked",
                issue_labels=issue_labels,
                pr_labels=pr_labels,
                blocking_labels=pr_blocking,
            )

        if label_manager.needs_rework in pr.labels:
            return ReviewValidity(
                valid=False,
                reason="pr_needs_rework",
                issue_labels=issue_labels,
                pr_labels=pr_labels,
            )

    if issue is None:
        return ReviewValidity(
            valid=True,
            reason="ok",
            pr_labels=pr_labels,
        )

    issue_blocking = tuple(label_manager.get_blocking(issue.labels))
    if issue_blocking:
        return ReviewValidity(
            valid=False,
            reason="issue_blocked",
            issue_labels=issue_labels,
            pr_labels=pr_labels,
            blocking_labels=issue_blocking,
        )

    if label_manager.needs_rework in issue.labels:
        return ReviewValidity(
            valid=False,
            reason="issue_needs_rework",
            issue_labels=issue_labels,
            pr_labels=pr_labels,
        )

    return ReviewValidity(
        valid=True,
        reason="ok",
        issue_labels=issue_labels,
        pr_labels=pr_labels,
    )
