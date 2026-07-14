"""Finalize a successful publish retry: labels, history, and review routing.

Split out of :class:`~.publish_recovery.PublishRecoveryService` so the "a PR now
exists — clear publish-failed state, record completed history, and route the PR
through the same review-discovery policy live completion uses" concern has one
named owner. Both retry entry points (recover-an-existing-PR and reconcile a
drained republish) delegate here, so they cannot drift apart on that policy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ..control.actions import AddLabelAction, RemoveLabelAction
from ..domain.models import DiscoveredReview, OrchestratorState, SessionHistoryEntry
from ..ports.fresh_issue_reader import FreshIssueReader
from .review_routing import should_queue_pr_review

logger = logging.getLogger(__name__)


class _ActionApplier(Protocol):
    def apply(self, action: AddLabelAction | RemoveLabelAction) -> Any: ...


class _LabelManager(Protocol):
    @property
    def publish_failed(self) -> str: ...
    @property
    def pr_pending(self) -> str: ...
    def extract_publish_fail_count(self, labels: list[str]) -> int: ...


@dataclass(frozen=True)
class RetryReviewRouting:
    """Session-level inputs for the "PR produced — queue review?" decision.

    Threaded into :meth:`RetrySuccessFinalizer.finalize` so a successful retry
    routes a PR through the same review-discovery policy as live completion
    instead of unconditionally finalizing to ``pr-pending``.
    """

    branch_name: str
    skip_review: bool
    review_exchange_completed: bool
    review_exchange_halted: bool


class RetrySuccessFinalizer:
    """Owns publish-failed cleanup + completed history + review routing."""

    def __init__(
        self,
        *,
        label_manager: _LabelManager,
        fresh_issue_reader: FreshIssueReader,
        action_applier: _ActionApplier,
        code_review_agent_configured: bool,
    ) -> None:
        self._lm = label_manager
        self._fresh_issue_reader = fresh_issue_reader
        self._action_applier = action_applier
        # Whether the repo has a code review agent configured. A successful retry
        # that produces a PR must route through the same review-discovery policy
        # as live completion, so a retry-published PR cannot bypass the review
        # gate. The planner still owns the dry-run / already-queued gates.
        self._code_review_agent_configured = code_review_agent_configured

    def finalize(
        self,
        *,
        state: OrchestratorState,
        issue_number: int,
        issue_title: str,
        agent_label: str | None,
        pr_url: str | None,
        pr_number: int | None,
        worktree_path: str | None,
        history_reason: str,
        routing: RetryReviewRouting,
    ) -> None:
        labels = tuple(self._current_labels(issue_number))
        # Decide review routing as a PURE step first (no state mutation): the
        # external label cleanup below can raise, and a half-applied finalize
        # must not leave review-discovery state queued for a PR whose
        # publish-failed cleanup never completed (split-brain).
        review_candidate = self._review_candidate(
            issue_number=issue_number,
            pr_url=pr_url,
            pr_number=pr_number,
            agent_label=agent_label,
            routing=routing,
        )
        label_actions = self._build_label_actions(
            issue_number=issue_number,
            labels=labels,
            pr_url=pr_url,
            will_queue_review=review_candidate is not None,
        )
        # Apply external label cleanup FIRST. If it raises, nothing below runs:
        # discovered_reviews stays untouched, no completed history is recorded,
        # and the caller never reaches its locator clear — the issue stays fully
        # in its publish-failed state and remains retryable.
        self._apply_label_actions(label_actions)
        if review_candidate is not None:
            state.discovered_reviews.append(review_candidate)
            logger.info(
                "[publish-retry] Routing issue=%s PR #%s through code-review "
                "discovery instead of finalizing to awaiting-merge",
                issue_number,
                review_candidate.pr_number,
            )
        self._record_completed_history(
            state,
            issue_number=issue_number,
            issue_title=issue_title,
            agent_label=agent_label,
            pr_url=pr_url,
            worktree_path=worktree_path,
            history_reason=history_reason,
            issue_labels=tuple(labels),
        )

    def _review_candidate(
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

    def _build_label_actions(
        self,
        *,
        issue_number: int,
        labels: tuple[str, ...],
        pr_url: str | None,
        will_queue_review: bool,
    ) -> list[AddLabelAction | RemoveLabelAction]:
        label_actions: list[AddLabelAction | RemoveLabelAction] = []
        if self._lm.publish_failed in labels:
            label_actions.append(
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=self._lm.publish_failed,
                    reason="retry publish succeeded",
                )
            )
        if not will_queue_review and pr_url and self._lm.pr_pending not in labels:
            # No review needed: finalize to awaiting-merge directly. When a review
            # IS queued, the planner owns pr-pending via the discovered review.
            label_actions.append(
                AddLabelAction(
                    issue_number=issue_number,
                    label=self._lm.pr_pending,
                    reason="publish retry recovered or succeeded",
                )
            )
        for label in labels:
            if self._lm.extract_publish_fail_count([label]) > 0:
                label_actions.append(
                    RemoveLabelAction(
                        issue_number=issue_number,
                        label=label,
                        reason="publish retry succeeded",
                    )
                )
        return label_actions

    def _record_completed_history(
        self,
        state: OrchestratorState,
        *,
        issue_number: int,
        issue_title: str,
        agent_label: str | None,
        pr_url: str | None,
        worktree_path: str | None,
        history_reason: str,
        issue_labels: tuple[str, ...],
    ) -> None:
        state.failed_this_cycle.discard(issue_number)
        state.discovered_failures = [
            failure for failure in state.discovered_failures
            if failure.issue_number != issue_number
        ]
        state.session_history = [
            entry for entry in state.session_history
            if not _is_publish_failure_history(entry, issue_number)
        ]
        if issue_number not in state.completed_today:
            state.completed_today.append(issue_number)
        state.session_history.append(
            SessionHistoryEntry(
                issue_number=issue_number,
                title=issue_title,
                agent_type=agent_label or "agent:unknown",
                status="completed",
                runtime_minutes=0,
                pr_url=pr_url,
                status_reason=history_reason,
                worktree_path=Path(worktree_path) if worktree_path else None,
                completed_at=datetime.now(timezone.utc),
                issue_labels=issue_labels,
            )
        )

    def _current_labels(self, issue_number: int) -> list[str]:
        return [str(label) for label in self._fresh_issue_reader.read_issue_labels(issue_number)]

    def _apply_label_actions(self, actions: list[AddLabelAction | RemoveLabelAction]) -> None:
        for action in actions:
            result = self._action_applier.apply(action)
            if result.success:
                continue
            raise RuntimeError(result.error or f"Label action failed for issue {action.issue_number}")


def _is_publish_failure_history(entry: SessionHistoryEntry, issue_number: int) -> bool:
    if entry.issue_number != issue_number:
        return False
    if entry.status not in {"blocked", "failed"}:
        return False
    reason = (entry.status_reason or "").strip().lower()
    if not reason:
        return False
    return (
        "push or pr creation failed" in reason
        or "publishing failed" in reason
        or "publish failed" in reason
    )
