"""Dependency evaluator - checks if issue dependencies are satisfied.

This controller evaluates dependencies and determines if an issue is runnable.
It's called before starting work sessions to gate on dependencies.

Architecture:
- Uses IssueTracker port to fetch dependency issue states
- Uses IssueResolver to translate external IDs to issue numbers
- Returns DependencyReport with runnable decision
- Emits events for observability
"""

import logging
from typing import Protocol, runtime_checkable

from ..domain.dependencies import (
    Dependency,
    DependencyReport,
    DependencyState,
    ParsedDependencyRef,
    parse_dependency_refs,
)
from ..domain.issue_key import GitHubIssueKey
from ..events import EventName
from ..ports import EventSink, TraceEvent, IssueResolver

logger = logging.getLogger(__name__)


@runtime_checkable
class IssueStateChecker(Protocol):
    """Protocol for checking issue state and milestone."""

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        """Get the state of an issue ('open', 'closed', or None if not found)."""
        ...

    def get_issue_milestone(self, issue_number: int, repo: str | None = None) -> str | None:
        """Get the milestone name of an issue (or None if no milestone)."""
        ...


class DependencyEvaluator:
    """Evaluates issue dependencies to determine if runnable.

    This is a control-plane component that:
    1. Parses dependency references from issue body
    2. Resolves external IDs (M1-010) to issue numbers via IssueResolver
    3. Checks each dependency's state via adapter
    4. Returns DependencyReport with runnable decision
    5. Emits trace events for observability

    No configuration options - dependencies block deterministically.
    """

    def __init__(
        self,
        issue_checker: IssueStateChecker,
        events: EventSink,
        issue_resolver: IssueResolver | None = None,
        repo: str | None = None,
        foundation_milestone: str = "M0",
    ):
        """Initialize the evaluator.

        Args:
            issue_checker: Adapter to check issue states and milestones
            events: EventSink for trace events
            issue_resolver: Optional resolver for external ID references
            repo: Repository in owner/repo format (required if issue_resolver is set)
            foundation_milestone: Milestone that any issue can depend on (default: M0)
        """
        self.issue_checker = issue_checker
        self.events = events
        self.issue_resolver = issue_resolver
        self.repo = repo
        self.foundation_milestone = foundation_milestone

    def evaluate(
        self,
        issue_number: int,
        issue_body: str,
        source_milestone: str | None = None,
    ) -> DependencyReport:
        """Evaluate dependencies for an issue.

        Args:
            issue_number: The issue being evaluated
            issue_body: The issue body text (contains Depends-on: lines)
            source_milestone: Milestone of the source issue (for cross-milestone validation)

        Returns:
            DependencyReport with runnable decision and dependency details
        """
        # Parse dependencies from body (supports both #123 and M1-010)
        dep_refs = parse_dependency_refs(issue_body)

        if not dep_refs:
            return DependencyReport(issue_number=issue_number)

        # Check each dependency
        satisfied: list[Dependency] = []
        unsatisfied: list[Dependency] = []
        missing: list[Dependency] = []
        unknown: list[Dependency] = []
        cross_milestone: list[Dependency] = []

        # If source issue has no milestone but declares dependencies, all deps are cross-milestone
        if source_milestone is None:
            for ref in dep_refs:
                dep = Dependency(
                    issue_number=ref.issue_number,
                    external_id=ref.external_id,
                    repository=ref.repository,
                    state=DependencyState.CROSS_MILESTONE,
                    error="Source issue has no milestone but declares dependencies",
                )
                cross_milestone.append(dep)
                logger.warning(
                    "Issue #%d has no milestone but declares dependency %s",
                    issue_number,
                    dep.display_ref,
                )
        else:
            for ref in dep_refs:
                dep = self._check_dependency_ref(ref, source_milestone)

                if dep.state == DependencyState.SATISFIED:
                    satisfied.append(dep)
                elif dep.state == DependencyState.UNSATISFIED:
                    unsatisfied.append(dep)
                elif dep.state == DependencyState.MISSING:
                    missing.append(dep)
                elif dep.state == DependencyState.CROSS_MILESTONE:
                    cross_milestone.append(dep)
                else:
                    unknown.append(dep)

        report = DependencyReport(
            issue_number=issue_number,
            satisfied=tuple(satisfied),
            unsatisfied=tuple(unsatisfied),
            missing=tuple(missing),
            unknown=tuple(unknown),
            cross_milestone=tuple(cross_milestone),
        )

        # Emit trace event
        self._emit_event(report)

        # Log warnings for missing/unknown/cross-milestone
        if report.has_warnings:
            for dep in report.missing:
                logger.warning(
                    "Issue #%d has missing dependency %s: %s",
                    issue_number,
                    dep.display_ref,
                    dep.error,
                )
            for dep in report.unknown:
                logger.warning(
                    "Issue #%d has unknown dependency %s: %s",
                    issue_number,
                    dep.display_ref,
                    dep.error,
                )
            for dep in report.cross_milestone:
                logger.warning(
                    "Issue #%d has cross-milestone dependency %s: %s",
                    issue_number,
                    dep.display_ref,
                    dep.error,
                )

        return report

    def _check_dependency_ref(
        self,
        ref: ParsedDependencyRef,
        source_milestone: str,
    ) -> Dependency:
        """Check the state of a parsed dependency reference.

        Handles both issue number refs (#123) and external ID refs (M1-010).
        External IDs are resolved to issue numbers via IssueResolver.
        Also validates milestone scope (same milestone or foundation).

        Args:
            ref: The parsed dependency reference
            source_milestone: Milestone of the source issue

        Returns:
            Dependency with resolved state.
        """
        # Resolve external ID to issue number if needed
        issue_number = ref.issue_number
        external_id = ref.external_id
        repo = ref.repository

        if external_id and issue_number is None:
            # Need to resolve external ID
            if self.issue_resolver is None or self.repo is None:
                logger.warning(
                    "External ID dependency %s cannot be resolved - no resolver configured",
                    external_id,
                )
                return Dependency(
                    issue_number=None,
                    external_id=external_id,
                    state=DependencyState.UNKNOWN,
                    error="No resolver configured for external ID references",
                )

            # Resolve via IssueResolver
            key = GitHubIssueKey(repo=self.repo, external_id=external_id)
            handle = self.issue_resolver.resolve(key)

            # For GitHub, handle is always int or None
            if handle is None:
                return Dependency(
                    issue_number=None,
                    external_id=external_id,
                    state=DependencyState.MISSING,
                    error=f"Could not resolve external ID {external_id} to issue number",
                )

            # GitHub resolver returns int handles
            if isinstance(handle, int):
                issue_number = handle
            else:
                # Non-int handle from non-GitHub resolver - not supported here
                return Dependency(
                    issue_number=None,
                    external_id=external_id,
                    state=DependencyState.UNKNOWN,
                    error=f"Resolver returned non-int handle: {type(handle).__name__}",
                )

        # Now check the state (issue_number should be set)
        if issue_number is None:
            return Dependency(
                issue_number=None,
                external_id=external_id,
                repository=repo,
                state=DependencyState.UNKNOWN,
                error="No issue number to check",
            )

        try:
            state = self.issue_checker.get_issue_state(issue_number, repo)
            dep_milestone = self.issue_checker.get_issue_milestone(issue_number, repo)

            if state is None:
                return Dependency(
                    issue_number=issue_number,
                    external_id=external_id,
                    repository=repo,
                    state=DependencyState.MISSING,
                    error="Issue not found or inaccessible",
                    milestone=dep_milestone,
                )

            # Check milestone scope before checking state
            # Dependencies must be: same milestone OR foundation milestone
            if dep_milestone is None:
                # Dependency has no milestone - treat as cross-milestone violation
                return Dependency(
                    issue_number=issue_number,
                    external_id=external_id,
                    repository=repo,
                    state=DependencyState.CROSS_MILESTONE,
                    error=f"Dependency has no milestone (source is in {source_milestone})",
                    milestone=dep_milestone,
                )
            is_same_milestone = dep_milestone == source_milestone
            is_foundation = dep_milestone == self.foundation_milestone
            if not is_same_milestone and not is_foundation:
                return Dependency(
                    issue_number=issue_number,
                    external_id=external_id,
                    repository=repo,
                    state=DependencyState.CROSS_MILESTONE,
                    error=f"Dependency in {dep_milestone}, not in {source_milestone} or {self.foundation_milestone}",
                    milestone=dep_milestone,
                )

            # Check open/closed state
            if state.lower() == "closed":
                return Dependency(
                    issue_number=issue_number,
                    external_id=external_id,
                    repository=repo,
                    state=DependencyState.SATISFIED,
                    milestone=dep_milestone,
                )
            else:
                return Dependency(
                    issue_number=issue_number,
                    external_id=external_id,
                    repository=repo,
                    state=DependencyState.UNSATISFIED,
                    milestone=dep_milestone,
                )

        except Exception as e:
            # Transient errors (network, rate limit, etc.)
            logger.debug(
                "Error checking dependency %s: %s",
                ref.external_id or f"#{issue_number}",
                e,
            )
            return Dependency(
                issue_number=issue_number,
                external_id=external_id,
                repository=repo,
                state=DependencyState.UNKNOWN,
                error=str(e),
            )

    def _emit_event(self, report: DependencyReport) -> None:
        """Emit trace event for dependency evaluation."""
        self.events.publish(
            TraceEvent(
                EventName.DEPENDENCIES_EVALUATED,
                {
                    "issue_number": report.issue_number,
                    "runnable": report.runnable,
                    "satisfied_count": len(report.satisfied),
                    "unsatisfied_count": len(report.unsatisfied),
                    "missing_count": len(report.missing),
                    "unknown_count": len(report.unknown),
                    "cross_milestone_count": len(report.cross_milestone),
                    "summary": report.summary(),
                },
            )
        )
