"""PR Scanner - discovers PRs needing review/rework.

This module scans GitHub for PRs that need attention:
- PRs with needs-code-review label (orphaned reviews)
- PRs with needs-rework label (changes requested)

It returns lists of PendingReview/PendingRework to be queued,
but does NOT modify state directly. The orchestrator decides
what to do with the results.
"""

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence, Protocol

from ..infra.config import Config
from ..events import EventName
from ..domain.models import PendingReview, PendingRework
from ..domain.issue_key import IssueKey
from ..domain.branch_naming import extract_issue_number_from_branch
from ..ports import EventSink, TraceEvent
from ..ports.pull_request_tracker import PRInfo
from ..infra import gh_audit

if TYPE_CHECKING:
    from ..ports.issue import Issue
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)


class RepositoryScanner(Protocol):
    """Protocol for repository scanning operations."""

    def get_prs_with_label(self, label: str) -> list[PRInfo]: ...
    def create_issue_key(self, issue_number: int) -> IssueKey: ...
    def get_issue(self, issue_number: int) -> "Issue | None": ...


@dataclass
class ScanResult:
    """Result of scanning for PRs."""

    reviews_to_queue: list[PendingReview]
    reworks_to_queue: list[PendingRework]
    escalations: list[tuple[int, int, int]]  # (pr_number, issue_number, rework_cycle)


class PRScanner:
    """Scans for PRs needing review or rework.

    This is a stateless scanner. It queries GitHub for PRs with specific
    labels and returns what should be queued, but does not modify any state.

    The orchestrator calls this periodically and decides whether to actually
    queue the discovered items (avoiding duplicates, checking capacity, etc.).
    """

    def __init__(
        self,
        config: Config,
        repository: RepositoryScanner,
        events: EventSink,
        label_manager: "LabelManager | None" = None,
    ):
        """Initialize the scanner.

        Args:
            config: Configuration with label settings
            repository: Adapter for GitHub operations
            events: EventSink for trace events
            label_manager: Label registry for prefix-aware queries.
        """
        self.config = config
        self.repository = repository
        self.events = events
        if label_manager is None:
            from .label_manager import LabelManager
            label_manager = LabelManager(config)
        self._lm = label_manager

    def scan_for_reviews(
        self,
        already_queued: Sequence[PendingReview],
        active_sessions: Sequence[str],  # session names
    ) -> list[PendingReview]:
        """Scan for PRs needing code review.

        Finds PRs with the code-review label that aren't already queued
        or being actively reviewed.

        Args:
            already_queued: Currently queued reviews (to avoid duplicates)
            active_sessions: Active session names (to skip PRs being reviewed)

        Returns:
            List of PendingReview for PRs that need to be queued
        """
        if not self.config.code_review_agent or not self.config.code_review_label:
            return []

        with gh_audit.context(
            reason=gh_audit.AuditReason.PR_SCAN,
            scope=gh_audit.AuditScope.PERIODIC,
        ):
            prs = self.repository.get_prs_with_label(self.config.code_review_label)
        results: list[PendingReview] = []

        queued_pr_numbers = {r.pr_number for r in already_queued}
        active_review_sessions = {s for s in active_sessions if s.startswith("review-")}

        for pr in prs:
            # Skip if already queued
            if pr.number in queued_pr_numbers:
                continue

            # Skip if already being reviewed
            session_name = f"review-{pr.number}"
            if session_name in active_review_sessions:
                continue

            # Extract issue number from PR body
            issue_number = self._extract_issue_number(pr.body, pr.number)

            review = PendingReview(
                issue_key=self.repository.create_issue_key(issue_number),
                pr_number=pr.number,
                pr_url=pr.url,
                branch_name=pr.branch,
                _issue_number=issue_number,
            )
            results.append(review)
            logger.info("[SCANNER] Found orphaned PR #%d for code review", pr.number)

        if results:
            self.events.publish(
                TraceEvent(
                    EventName.SCANNER_REVIEWS_FOUND,
                    {"count": len(results)},
                )
            )

        return results

    def scan_for_reworks(
        self,
        already_queued: Sequence[PendingRework],
        active_sessions: Sequence[int],  # issue numbers being worked on
    ) -> tuple[list[PendingRework], list[tuple[int, int, int]]]:
        """Scan for PRs needing rework.

        Finds PRs with the needs-rework label that aren't already queued
        or being actively worked on.

        Args:
            already_queued: Currently queued reworks (to avoid duplicates)
            active_sessions: Issue numbers of active work sessions

        Returns:
            Tuple of (reworks to queue, escalations needed)
            Escalations are (pr_number, issue_number, rework_cycle) tuples
        """
        if not self.config.code_review_agent:
            return [], []

        rework_label = self._lm.needs_rework
        with gh_audit.context(
            reason=gh_audit.AuditReason.PR_SCAN,
            scope=gh_audit.AuditScope.PERIODIC,
        ):
            prs = self.repository.get_prs_with_label(rework_label)
        logger.info("[SCANNER] Found %d PRs with '%s' label", len(prs), rework_label)

        results: list[PendingRework] = []
        escalations: list[tuple[int, int, int]] = []

        queued_issue_ids = {
            r.resolve_issue_number()
            for r in already_queued
            if r.resolve_issue_number() is not None
        }
        active_issue_numbers = set(active_sessions)

        for pr in prs:
            # Extract issue number from branch name (reliable, orchestrator-controlled)
            # Fall back to PR body parsing if branch doesn't match pattern
            issue_number = self._extract_issue_number_from_pr(pr)

            # Skip if already queued
            if issue_number in queued_issue_ids:
                continue

            # Skip if already being worked on
            if issue_number in active_issue_numbers:
                continue

            rework_cycle = self._get_rework_cycle_from_labels(pr.labels)

            # Skip if already blocked (has any blocking label like blocked-*, needs-human, etc.)
            # This prevents escalation spam when GitHub label cache is stale
            if self._lm.is_blocking_any(pr.labels):
                blocking = self._lm.get_blocking(pr.labels)
                logger.debug(
                    "[SCANNER] PR #%d already blocked (%s), skipping",
                    pr.number, ", ".join(blocking)
                )
                continue

            # Check if exceeded max rework cycles
            if rework_cycle > self.config.max_rework_cycles:
                escalations.append((pr.number, issue_number, rework_cycle))
                continue

            # Look up issue to get agent type (issue is source of truth)
            issue = self.repository.get_issue(issue_number)
            if not issue:
                logger.warning(
                    "[SCANNER] PR #%d references issue #%d which doesn't exist, skipping",
                    pr.number, issue_number
                )
                continue

            agent_type = issue.agent_type
            if not agent_type:
                logger.warning(
                    "[SCANNER] Issue #%d has no agent label, skipping PR #%d",
                    issue_number, pr.number
                )
                continue

            # Create IssueKey via adapter
            issue_key = self.repository.create_issue_key(issue_number)

            rework = PendingRework(
                issue_key=issue_key,
                agent_type=agent_type,
                rework_cycle=rework_cycle,
                issue_number=issue_number,
            )
            results.append(rework)
            logger.info("[SCANNER] Found PR #%d for rework (cycle %d)", pr.number, rework_cycle)

        if results or escalations:
            self.events.publish(
                TraceEvent(
                    EventName.SCANNER_REWORKS_FOUND,
                    {
                        "reworks": len(results),
                        "escalations": len(escalations),
                    },
                )
            )

        return results, escalations

    def _extract_issue_number_from_pr(self, pr: PRInfo) -> int:
        """Extract issue number from PR, preferring branch name over body.

        The branch name is more reliable as it's set by the orchestrator
        and agents can't modify it. Falls back to PR body parsing if
        branch doesn't match the expected pattern.

        Args:
            pr: The PR to extract issue number from

        Returns:
            Issue number, falling back to PR number if not found
        """
        # Try branch name first (format: {issue_number}-{slug})
        if pr.branch:
            issue_from_branch = extract_issue_number_from_branch(pr.branch)
            if issue_from_branch is not None:
                return issue_from_branch

        # Fall back to PR body parsing
        return self._extract_issue_number(pr.body, pr.number)

    def _extract_issue_number(self, pr_body: str, fallback: int) -> int:
        """Extract issue number from PR body (Closes #N pattern)."""
        match = re.search(r'Closes #(\d+)', pr_body, re.IGNORECASE)
        return int(match.group(1)) if match else fallback

    def _get_rework_cycle_from_labels(self, labels: list[str]) -> int:
        """Extract rework cycle count from labels (rework-cycle-N).

        Returns the NEXT cycle number (e.g., rework-cycle-2 means next is cycle 3).
        """
        cycle = self._lm.extract_rework_cycle(labels)
        if cycle is not None:
            return cycle + 1  # Next cycle
        return 1  # First rework

