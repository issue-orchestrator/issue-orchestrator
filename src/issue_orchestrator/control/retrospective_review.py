"""Retrospective review workflow helpers.

This module owns the policy for reviewing an existing implementation before
deciding whether coder rework is needed. It is intentionally distinct from
reset/retry: it labels and queues review work, and it does not reopen issues,
delete branches, remove worktrees, or start a coder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.models import (
    DiscoveredRetrospectiveReview,
    ORCHESTRATOR_PR_MARKER,
    PendingRetrospectiveReview,
)

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports.issue import Issue
    from ..ports.repository_host import RepositoryHost

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrospectiveReviewIssueDecision:
    """Preflight result for one retrospective-review request."""

    issue: int
    title: str | None
    state: str | None
    labels: list[str]
    eligible: bool
    action: str
    reason: str
    agent_label: str | None = None
    trigger_label: str | None = None
    prior_pr_number: int | None = None
    prior_pr_url: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "issue": self.issue,
            "title": self.title,
            "state": self.state,
            "labels": self.labels,
            "eligible": self.eligible,
            "action": self.action,
            "reason": self.reason,
            "agent_label": self.agent_label,
            "trigger_label": self.trigger_label,
            "prior_pr_number": self.prior_pr_number,
            "prior_pr_url": self.prior_pr_url,
        }


def preflight_retrospective_review_issues(
    *,
    issue_numbers: list[int],
    repository_host: "RepositoryHost",
    config: "Config",
) -> list[RetrospectiveReviewIssueDecision]:
    """Preview retrospective-review decisions without mutating GitHub/state."""

    decisions: list[RetrospectiveReviewIssueDecision] = []
    for number in issue_numbers:
        try:
            issue = repository_host.get_issue(number)
        except Exception as exc:
            logger.error(
                "[retrospective-review] Failed preflight for issue #%d: %s",
                number,
                exc,
                exc_info=True,
            )
            decisions.append(_skip(number, f"Unable to fetch issue: {exc}"))
            continue
        decisions.append(
            preflight_retrospective_review_issue(
                number,
                issue,
                repository_host,
                config,
            )
        )
    return decisions


def preflight_retrospective_review_issue(
    number: int,
    issue: "Issue | None",
    repository_host: "RepositoryHost",
    config: "Config",
) -> RetrospectiveReviewIssueDecision:
    """Return the UI/API decision for a single issue."""

    if issue is None:
        return _skip(number, f"Issue #{number} not found")

    marks = list(issue.labels)
    current = str(issue.state or "open").lower()
    title = issue.title
    trigger_label = config.retrospective_review_trigger_label

    if not config.retrospective_review_enabled:
        return _decision_skip(
            issue,
            trigger_label,
            "Retrospective review workflow is disabled",
        )
    if not trigger_label:
        return _decision_skip(issue, trigger_label, "Retrospective review trigger label is empty")
    if not config.code_review_agent:
        return _decision_skip(issue, trigger_label, "No code review agent is configured")

    agent_detail = _agent_skip_detail(config, issue)
    if agent_detail is not None:
        return _decision_skip(issue, trigger_label, agent_detail)

    prior_pr_number, prior_pr_url = find_orchestrator_pr_for_issue(repository_host, number)
    return RetrospectiveReviewIssueDecision(
        issue=number,
        title=title,
        state=current,
        labels=marks,
        eligible=True,
        action="queue_review",
        reason=(
            "Closed issue will stay closed unless retrospective review requests changes"
            if current == "closed"
            else "Issue will be queued for retrospective review"
        ),
        agent_label=issue.agent_type,
        trigger_label=trigger_label,
        prior_pr_number=prior_pr_number,
        prior_pr_url=prior_pr_url,
    )


def retrospective_review_preflight_payload(
    decisions: list[RetrospectiveReviewIssueDecision],
    *,
    trigger_label: str,
) -> dict[str, object]:
    """Build the stable response payload for preflight endpoints."""

    eligible = [decision.issue for decision in decisions if decision.eligible]
    skipped = [decision.issue for decision in decisions if not decision.eligible]
    return {
        "decisions": [decision.to_payload() for decision in decisions],
        "eligible": eligible,
        "skipped": skipped,
        "workflow": "retrospective_review",
        "trigger_label": trigger_label,
    }


def queue_retrospective_review_request(
    *,
    state: "OrchestratorState",
    repository_host: "RepositoryHost",
    decision: RetrospectiveReviewIssueDecision,
) -> bool:
    """Queue an eligible retrospective review in memory if not already queued."""

    if not decision.eligible or not decision.agent_label or not decision.trigger_label:
        return False
    issue_number = decision.issue
    if state.has_in_flight_retrospective_review(issue_number):
        return False

    state.pending_retrospective_reviews.append(
        PendingRetrospectiveReview(
            issue_key=repository_host.create_issue_key(issue_number),
            issue_number=issue_number,
            issue_title=decision.title or f"Issue #{issue_number}",
            agent_label=decision.agent_label,
            trigger_label=decision.trigger_label,
            prior_pr_number=decision.prior_pr_number,
            prior_pr_url=decision.prior_pr_url,
            issue_labels=tuple(decision.labels),
        )
    )
    return True


def discover_retrospective_review_issues(
    *,
    repository_host: "RepositoryHost",
    config: "Config",
    already_issue_numbers: set[int],
) -> list[DiscoveredRetrospectiveReview]:
    """Discover trigger-labeled issues for review-first existing-work audits."""

    if not (
        config.retrospective_review_enabled
        and config.retrospective_review_trigger_label
        and config.code_review_agent
    ):
        return []

    trigger_label = config.retrospective_review_trigger_label
    discovered: list[DiscoveredRetrospectiveReview] = []
    issues = repository_host.list_issues(
        labels=[trigger_label],
        state="all",
        limit=config.filtering.fetch_limit,
    )
    for issue in issues:
        if issue.number in already_issue_numbers:
            continue
        agent_label = issue.agent_type
        if not agent_label or agent_label not in config.agents:
            logger.info(
                "[retrospective-review] Skipping issue #%d: missing or unconfigured agent label %s",
                issue.number,
                agent_label or "(none)",
            )
            continue
        prior_pr_number, prior_pr_url = find_orchestrator_pr_for_issue(
            repository_host,
            issue.number,
        )
        discovered.append(
            DiscoveredRetrospectiveReview(
                issue_number=issue.number,
                issue_title=issue.title,
                agent_label=agent_label,
                trigger_label=trigger_label,
                issue_key=issue.key.stable_id(),
                prior_pr_number=prior_pr_number,
                prior_pr_url=prior_pr_url,
                issue_labels=tuple(issue.labels),
            )
        )
    return discovered


def build_retrospective_review_existing_work(
    review: PendingRetrospectiveReview,
) -> str:
    """Build explicit task context for retrospective-review sessions."""

    prior_pr = (
        f"Prior orchestrator PR: #{review.prior_pr_number} {review.prior_pr_url}."
        if review.prior_pr_number and review.prior_pr_url
        else "No prior orchestrator PR with the expected signature was found."
    )
    return (
        "RETROSPECTIVE REVIEW MODE: audit the existing implementation for "
        f"issue #{review.issue_number}. The issue may already be closed or say "
        "the work is done; that is expected and is not a reason to stop. "
        "Review the current repository state, tests, issue context, and prior "
        "orchestrator PR context if available. If the existing implementation "
        "still satisfies the issue under the current codebase and current review "
        "standards, call reviewer-done approved. If it needs changes, call "
        "reviewer-done changes_requested with concrete coder instructions. "
        "Do not modify code, push branches, delete worktrees, or mutate GitHub. "
        f"{prior_pr}"
    )


def find_orchestrator_pr_for_issue(
    repository_host: "RepositoryHost",
    issue_number: int,
) -> tuple[int | None, str | None]:
    """Return the first PR for an issue with the orchestrator signature."""

    prs = repository_host.get_prs_for_issue(issue_number, state="all")
    for pr in prs:
        if ORCHESTRATOR_PR_MARKER in (pr.body or ""):
            return pr.number, pr.url
    return None, None


def _agent_skip_detail(config: "Config", issue: "Issue") -> str | None:
    agent_marker = issue.agent_type
    if not agent_marker:
        return f"Issue #{issue.number} has no agent:* label"
    if agent_marker not in config.agents:
        return f'Issue #{issue.number} uses unconfigured agent label "{agent_marker}"'
    return None


def _decision_skip(
    issue: "Issue",
    trigger_label: str | None,
    reason: str,
) -> RetrospectiveReviewIssueDecision:
    return RetrospectiveReviewIssueDecision(
        issue=issue.number,
        title=issue.title,
        state=str(issue.state or "open").lower(),
        labels=list(issue.labels),
        eligible=False,
        action="skipped",
        reason=reason,
        agent_label=issue.agent_type,
        trigger_label=trigger_label,
    )


def _skip(number: int, reason: str) -> RetrospectiveReviewIssueDecision:
    return RetrospectiveReviewIssueDecision(
        issue=number,
        title=None,
        state=None,
        labels=[],
        eligible=False,
        action="skipped",
        reason=reason,
    )
