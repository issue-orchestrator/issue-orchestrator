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
from ..ports import EventSink,  make_trace_event
from ..ports.pull_request_tracker import PRInfo
from ..infra import gh_audit
from ..infra.timeline_trace import is_timeline_trace_enabled

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


@dataclass(frozen=True)
class _ReworkScanDecision:
    decision: str  # "skip" | "queue" | "escalate"
    issue_number: int
    rework_cycle: int
    blocking_labels: list[str]
    reason: str


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

            # Skip PRs whose linked issue is outside configured scope
            if not self._matches_issue_scope(issue_number, pr.number):
                continue

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
                make_trace_event(
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

        queued_issue_ids = self._collect_queued_issue_ids(already_queued)
        active_issue_numbers = set(active_sessions)

        for pr in prs:
            decision = self._decide_rework_candidate(pr, queued_issue_ids, active_issue_numbers)
            self._log_rework_decision(pr, decision, queued_issue_ids, active_issue_numbers)
            if decision.decision == "skip":
                continue
            if decision.decision == "escalate":
                escalations.append((pr.number, decision.issue_number, decision.rework_cycle))
                continue

            issue = self.repository.get_issue(decision.issue_number)
            if not issue:
                logger.warning(
                    "[SCANNER] PR #%d references issue #%d which doesn't exist, skipping",
                    pr.number, decision.issue_number
                )
                continue
            agent_type = issue.agent_type
            if not agent_type:
                logger.warning(
                    "[SCANNER] Issue #%d has no agent label, skipping PR #%d",
                    decision.issue_number, pr.number
                )
                continue
            results.append(
                PendingRework(
                    issue_key=self.repository.create_issue_key(decision.issue_number),
                    agent_type=agent_type,
                    rework_cycle=decision.rework_cycle,
                    issue_number=decision.issue_number,
                    pr_number=pr.number,
                )
            )
            logger.info("[SCANNER] Found PR #%d for rework (cycle %d)", pr.number, decision.rework_cycle)
            if is_timeline_trace_enabled():
                logger.info(
                    "[TIMELINE] scanner.rework_queue pr=%s issue=%s cycle=%s agent=%s",
                    pr.number,
                    decision.issue_number,
                    decision.rework_cycle,
                    agent_type,
                )

        if results or escalations:
            self.events.publish(
                make_trace_event(
                    EventName.SCANNER_REWORKS_FOUND,
                    {
                        "reworks": len(results),
                        "escalations": len(escalations),
                    },
                )
            )

        return results, escalations

    @staticmethod
    def _collect_queued_issue_ids(already_queued: Sequence[PendingRework]) -> set[int]:
        queued_issue_ids: set[int] = set()
        for rework in already_queued:
            issue_number = rework.resolve_issue_number()
            if issue_number is not None:
                queued_issue_ids.add(issue_number)
        return queued_issue_ids

    def _decide_rework_candidate(
        self,
        pr: PRInfo,
        queued_issue_ids: set[int],
        active_issue_numbers: set[int],
    ) -> _ReworkScanDecision:
        issue_number = self._extract_issue_number_from_pr(pr)

        # Skip PRs whose linked issue is outside configured scope
        if not self._matches_issue_scope(issue_number, pr.number):
            return _ReworkScanDecision(
                decision="skip",
                issue_number=issue_number,
                rework_cycle=0,
                blocking_labels=[],
                reason="out_of_scope",
            )

        if issue_number in queued_issue_ids:
            return _ReworkScanDecision(
                decision="skip",
                issue_number=issue_number,
                rework_cycle=0,
                blocking_labels=[],
                reason="already_queued",
            )
        if issue_number in active_issue_numbers:
            return _ReworkScanDecision(
                decision="skip",
                issue_number=issue_number,
                rework_cycle=0,
                blocking_labels=[],
                reason="active_session",
            )
        rework_cycle = self._get_rework_cycle_from_labels(pr.labels)
        if self._lm.is_blocking_any(pr.labels):
            return _ReworkScanDecision(
                decision="skip",
                issue_number=issue_number,
                rework_cycle=rework_cycle,
                blocking_labels=self._lm.get_blocking(pr.labels),
                reason="blocking_label",
            )
        # Also check the linked issue's labels — a publish failure marks the
        # issue as blocked-failed but may leave needs-rework on the PR.
        issue = self.repository.get_issue(issue_number)
        if issue is not None and self._lm.is_blocking_any(issue.labels):
            return _ReworkScanDecision(
                decision="skip",
                issue_number=issue_number,
                rework_cycle=rework_cycle,
                blocking_labels=self._lm.get_blocking(issue.labels),
                reason="issue_blocked",
            )
        if rework_cycle > self.config.max_rework_cycles:
            return _ReworkScanDecision(
                decision="escalate",
                issue_number=issue_number,
                rework_cycle=rework_cycle,
                blocking_labels=[],
                reason="max_rework_exceeded",
            )
        return _ReworkScanDecision(
            decision="queue",
            issue_number=issue_number,
            rework_cycle=rework_cycle,
            blocking_labels=[],
            reason="queue",
        )

    def _log_rework_decision(
        self,
        pr: PRInfo,
        decision: _ReworkScanDecision,
        queued_issue_ids: set[int],
        active_issue_numbers: set[int],
    ) -> None:
        if decision.reason == "blocking_label" and decision.blocking_labels:
            logger.debug(
                "[SCANNER] PR #%d already blocked (%s), skipping",
                pr.number,
                ", ".join(decision.blocking_labels),
            )
        if not is_timeline_trace_enabled():
            return
        logger.info(
            "[TIMELINE] scanner.rework_candidate pr=%s issue=%s labels=%s queued=%s active=%s",
            pr.number,
            decision.issue_number,
            ",".join(pr.labels),
            decision.issue_number in queued_issue_ids,
            decision.issue_number in active_issue_numbers,
        )
        if decision.decision == "skip":
            extra = (
                f" blocking={','.join(decision.blocking_labels)}"
                if decision.reason == "blocking_label" and decision.blocking_labels
                else ""
            )
            logger.info(
                "[TIMELINE] scanner.rework_skip pr=%s issue=%s reason=%s%s",
                pr.number,
                decision.issue_number,
                decision.reason,
                extra,
            )
            return
        if decision.decision == "escalate":
            logger.info(
                "[TIMELINE] scanner.rework_escalate pr=%s issue=%s cycle=%s max=%s",
                pr.number,
                decision.issue_number,
                decision.rework_cycle,
                self.config.max_rework_cycles,
            )

    def _matches_issue_scope(self, issue_number: int, pr_number: int) -> bool:
        """Check if the linked issue is within the configured scope.

        Enforces three scope checks:
        1. filtering.issue — single-issue scope (no API call needed)
        2. filtering.label — include filter (requires issue fetch)
        3. exclude_labels — exclusion filter (requires issue fetch)

        Without these checks, the PR scanner would pick up PRs for issues
        outside the configured scope (e.g., e2e test issues leaking into
        production orchestrators).
        """
        # Single-issue scope: cheap check, no API call
        if self.config.filtering.issue and issue_number != self.config.filtering.issue:
            logger.debug(
                "[SCANNER] PR #%d linked to issue #%d outside single-issue scope (%d), skipping",
                pr_number, issue_number, self.config.filtering.issue,
            )
            return False

        filter_label = self.config.filtering.label
        issue_filter = self.config.get_issue_filter()

        # If neither include nor exclude filter is configured, pass
        if not filter_label and issue_filter.is_empty():
            return True

        issue = self.repository.get_issue(issue_number)
        if issue is None:
            logger.info(
                "[SCANNER] PR #%d linked to issue #%d which doesn't exist, skipping",
                pr_number, issue_number,
            )
            return False

        # Include filter
        if filter_label and filter_label not in issue.labels:
            logger.debug(
                "[SCANNER] PR #%d linked to issue #%d without filter label '%s', skipping",
                pr_number, issue_number, filter_label,
            )
            return False

        # Exclude filter
        if not issue_filter.apply([issue]):
            logger.debug(
                "[SCANNER] PR #%d linked to issue #%d excluded by label filter, skipping",
                pr_number, issue_number,
            )
            return False

        return True

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
