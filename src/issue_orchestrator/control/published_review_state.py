"""Idempotent live-state replay for a durably published issue/PR pair."""

from datetime import datetime
from pathlib import Path

from ..domain.models import DiscoveredReview, SessionHistoryEntry
from ..domain.published_work_finalization import PublishedWorkFinalizationRequest
from ..ports.validated_work_effects import ValidatedWorkEffectAuthority


class PublishedReviewState:
    """Own each state mutation and its fence; replay never clears unrelated failures."""

    def __init__(self, effects: ValidatedWorkEffectAuthority) -> None:
        self._effects = effects

    def replay(
        self,
        request: PublishedWorkFinalizationRequest,
        candidate: DiscoveredReview | None,
        *,
        completed_at: datetime,
    ) -> None:
        state, target = request.state, request.target
        if candidate is not None:
            self._effects.perform(
                request.execution_token,
                request.claim,
                lambda: state.record_discovered_review(candidate),
            )
        entry = SessionHistoryEntry(
            issue_number=target.key.issue_number,
            title=request.issue_title,
            agent_type=request.agent_label or "agent:unknown",
            status="completed",
            runtime_minutes=0,
            pr_url=target.pr_url,
            status_reason=request.history_reason,
            worktree_path=Path(request.worktree_path)
            if request.worktree_path
            else None,
            completed_at=completed_at,
            issue_labels=request.observed_blocking_labels,
        )
        self._effects.perform(
            request.execution_token,
            request.claim,
            lambda: state.record_publication_history(entry),
        )
        self._effects.perform(
            request.execution_token,
            request.claim,
            lambda: state.record_completed_issue(target.key.issue_number),
        )
