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
from typing import Sequence, Protocol

from ..infra.config import Config
from ..events import EventName
from ..models import PendingReview, PendingRework
from ..domain.issue_key import IssueKey
from ..ports import EventSink, TraceEvent
from ..ports.pull_request_tracker import PRInfo
from ..infra import gh_audit

logger = logging.getLogger(__name__)


class RepositoryScanner(Protocol):
    """Protocol for repository scanning operations."""

    def get_prs_with_label(self, label: str) -> list[PRInfo]: ...
    def create_issue_key(self, issue_number: int) -> IssueKey: ...


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
    ):
        """Initialize the scanner.

        Args:
            config: Configuration with label settings
            repository: Adapter for GitHub operations
            events: EventSink for trace events
        """
        self.config = config
        self.repository = repository
        self.events = events

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

        rework_label = self.config.get_label_needs_rework()
        with gh_audit.context(
            reason=gh_audit.AuditReason.PR_SCAN,
            scope=gh_audit.AuditScope.PERIODIC,
        ):
            prs = self.repository.get_prs_with_label(rework_label)
        logger.info("[SCANNER] Found %d PRs with '%s' label", len(prs), rework_label)

        results: list[PendingRework] = []
        escalations: list[tuple[int, int, int]] = []

        queued_issue_ids = {int(r.issue_key.stable_id()) for r in already_queued}
        active_issue_numbers = set(active_sessions)

        for pr in prs:
            issue_number = self._extract_issue_number(pr.body, pr.number)

            # Skip if already queued
            if issue_number in queued_issue_ids:
                continue

            # Skip if already being worked on
            if issue_number in active_issue_numbers:
                continue

            _branch_name = pr.branch or f"{issue_number}-rework"
            rework_cycle = self._get_rework_cycle_from_labels(pr.labels)

            # Check if exceeded max rework cycles
            if rework_cycle > self.config.max_rework_cycles:
                escalations.append((pr.number, issue_number, rework_cycle))
                continue

            # Extract agent type from labels
            agent_type = self._extract_agent_type(pr.labels)
            if not agent_type:
                logger.warning("[SCANNER] PR #%d has no agent label, skipping", pr.number)
                continue

            # Create IssueKey via adapter
            issue_key = self.repository.create_issue_key(issue_number)

            rework = PendingRework(
                issue_key=issue_key,
                agent_type=agent_type,
                rework_cycle=rework_cycle,
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

    def _extract_issue_number(self, pr_body: str, fallback: int) -> int:
        """Extract issue number from PR body (Closes #N pattern)."""
        match = re.search(r'Closes #(\d+)', pr_body, re.IGNORECASE)
        return int(match.group(1)) if match else fallback

    def _get_rework_cycle_from_labels(self, labels: list[str]) -> int:
        """Extract rework cycle count from labels (rework-cycle-N).

        Returns the NEXT cycle number (e.g., rework-cycle-2 means next is cycle 3).
        """
        for label in labels:
            match = re.match(r"rework-cycle-(\d+)", label)
            if match:
                return int(match.group(1)) + 1  # Next cycle
        return 1  # First rework

    def _extract_agent_type(self, labels: list[str]) -> str | None:
        """Extract agent type from labels (agent:xxx)."""
        for label in labels:
            if label.startswith("agent:"):
                return label
        return None
