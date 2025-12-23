"""Dependency evaluator - checks if issue dependencies are satisfied.

This controller evaluates dependencies and determines if an issue is runnable.
It's called before starting work sessions to gate on dependencies.

Architecture:
- Uses IssueTracker port to fetch dependency issue states
- Returns DependencyReport with runnable decision
- Emits events for observability
"""

import logging
from typing import Protocol, runtime_checkable

from ..domain.dependencies import (
    Dependency,
    DependencyReport,
    DependencyState,
    parse_dependencies,
)
from ..ports import EventSink, TraceEvent

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
    2. Checks each dependency's state via adapter
    3. Returns DependencyReport with runnable decision
    4. Emits trace events for observability

    No configuration options - dependencies block deterministically.
    """

    def __init__(
        self,
        issue_checker: IssueStateChecker,
        events: EventSink,
    ):
        """Initialize the evaluator.

        Args:
            issue_checker: Adapter to check issue states
            events: EventSink for trace events
        """
        self.issue_checker = issue_checker
        self.events = events

    def evaluate(self, issue_number: int, issue_body: str) -> DependencyReport:
        """Evaluate dependencies for an issue.

        Args:
            issue_number: The issue being evaluated
            issue_body: The issue body text (contains Depends-on: lines)

        Returns:
            DependencyReport with runnable decision and dependency details
        """
        # Parse dependencies from body
        dep_refs = parse_dependencies(issue_body)

        if not dep_refs:
            return DependencyReport(issue_number=issue_number)

        # Check each dependency
        satisfied: list[Dependency] = []
        unsatisfied: list[Dependency] = []
        missing: list[Dependency] = []
        unknown: list[Dependency] = []

        for dep_issue, dep_repo in dep_refs:
            dep = self._check_dependency(dep_issue, dep_repo)

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
                    "Issue #%d has missing dependency #%d: %s",
                    issue_number,
                    dep.issue_number,
                    dep.error,
                )
            for dep in report.unknown:
                logger.warning(
                    "Issue #%d has unknown dependency #%d: %s",
                    issue_number,
                    dep.issue_number,
                    dep.error,
                )

        return report

    def _check_dependency(self, issue_number: int, repo: str | None) -> Dependency:
        """Check the state of a single dependency.

        Returns Dependency with resolved state.
        """
        try:
            state = self.issue_checker.get_issue_state(issue_number, repo)

            if state is None:
                return Dependency(
                    issue_number=issue_number,
                    repository=repo,
                    state=DependencyState.MISSING,
                    error="Issue not found or inaccessible",
                )
            elif state.lower() == "closed":
                return Dependency(
                    issue_number=issue_number,
                    repository=repo,
                    state=DependencyState.SATISFIED,
                )
            else:
                return Dependency(
                    issue_number=issue_number,
                    repository=repo,
                    state=DependencyState.UNSATISFIED,
                )

        except Exception as e:
            # Transient errors (network, rate limit, etc.)
            logger.debug(
                "Error checking dependency #%d: %s",
                issue_number,
                e,
            )
            return Dependency(
                issue_number=issue_number,
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
