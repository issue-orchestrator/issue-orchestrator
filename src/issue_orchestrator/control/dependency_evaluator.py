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
from ..ports import EventSink, TraceEvent, IssueResolver

logger = logging.getLogger(__name__)


@runtime_checkable
class IssueStateChecker(Protocol):
    """Protocol for checking issue state."""

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        """Get the state of an issue ('open', 'closed', or None if not found)."""
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
    ):
        """Initialize the evaluator.

        Args:
            issue_checker: Adapter to check issue states
            events: EventSink for trace events
            issue_resolver: Optional resolver for external ID references
            repo: Repository in owner/repo format (required if issue_resolver is set)
        """
        self.issue_checker = issue_checker
        self.events = events
        self.issue_resolver = issue_resolver
        self.repo = repo

    def evaluate(self, issue_number: int, issue_body: str) -> DependencyReport:
        """Evaluate dependencies for an issue.

        Args:
            issue_number: The issue being evaluated
            issue_body: The issue body text (contains Depends-on: lines)

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

        for ref in dep_refs:
            dep = self._check_dependency_ref(ref)

            if dep.state == DependencyState.SATISFIED:
                satisfied.append(dep)
            elif dep.state == DependencyState.UNSATISFIED:
                unsatisfied.append(dep)
            elif dep.state == DependencyState.MISSING:
                missing.append(dep)
            else:
                unknown.append(dep)

        report = DependencyReport(
            issue_number=issue_number,
            satisfied=tuple(satisfied),
            unsatisfied=tuple(unsatisfied),
            missing=tuple(missing),
            unknown=tuple(unknown),
        )

        # Emit trace event
        self._emit_event(report)

        # Log warnings for missing/unknown
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

        return report

    def _check_dependency_ref(self, ref: ParsedDependencyRef) -> Dependency:
        """Check the state of a parsed dependency reference.

        Handles both issue number refs (#123) and external ID refs (M1-010).
        External IDs are resolved to issue numbers via IssueResolver.

        Returns Dependency with resolved state.
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

            if state is None:
                return Dependency(
                    issue_number=issue_number,
                    external_id=external_id,
                    repository=repo,
                    state=DependencyState.MISSING,
                    error="Issue not found or inaccessible",
                )
            elif state.lower() == "closed":
                return Dependency(
                    issue_number=issue_number,
                    external_id=external_id,
                    repository=repo,
                    state=DependencyState.SATISFIED,
                )
            else:
                return Dependency(
                    issue_number=issue_number,
                    external_id=external_id,
                    repository=repo,
                    state=DependencyState.UNSATISFIED,
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
                name="dependencies.evaluated",
                data={
                    "issue_number": report.issue_number,
                    "runnable": report.runnable,
                    "satisfied_count": len(report.satisfied),
                    "unsatisfied_count": len(report.unsatisfied),
                    "missing_count": len(report.missing),
                    "unknown_count": len(report.unknown),
                    "summary": report.summary(),
                },
            )
        )
