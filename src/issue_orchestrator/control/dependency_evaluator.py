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
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Protocol, runtime_checkable

from ..domain.dependencies import (
    Dependency,
    DependencyMode,
    DependencyReport,
    DependencyState,
    EdgeProblem,
    ParsedDependencyRef,
    parse_dependency_edges,
    parse_dependency_refs,
)
from ..domain.dependency_gates import (
    DependencyGateReport,
    PredecessorFacts,
    build_gate_report,
    detect_cycles,
)
from ..domain.issue_key import GitHubIssueKey
from ..events import EventName
from ..ports import EventSink,  make_trace_event, IssueResolver
from ..ports.repository_host import RepositoryHostError

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
        """Evaluate dependencies for an issue."""
        dep_refs = parse_dependency_refs(issue_body)
        if not dep_refs:
            return DependencyReport(issue_number=issue_number)

        # Check each dependency
        dep_lists = self._check_all_dependencies(dep_refs, source_milestone, issue_number)

        report = DependencyReport(
            issue_number=issue_number,
            satisfied=tuple(dep_lists["satisfied"]),
            unsatisfied=tuple(dep_lists["unsatisfied"]),
            missing=tuple(dep_lists["missing"]),
            unknown=tuple(dep_lists["unknown"]),
            cross_milestone=tuple(dep_lists["cross_milestone"]),
        )

        self._emit_event(report)
        self._log_dependency_warnings(report, issue_number)
        return report

    def evaluate_gates(
        self,
        issue_number: int,
        issue_body: str,
        source_milestone: str | None = None,
        *,
        predecessor_facts: Mapping[int, PredecessorFacts] | None = None,
        same_stack_members: frozenset[int] = frozenset(),
        approval_current: bool = True,
        configured_base_branch: str | None = None,
        dependency_graph: Mapping[int, Sequence[int]] | None = None,
    ) -> DependencyGateReport:
        """Evaluate typed dependency edges into a dependency gate report.

        Parses both ``Depends-on:`` (normal) and ``Stack-after:`` (stack) edges,
        resolves and checks each, annotates structural problems (self-dependency,
        duplicates, mode conflicts, cycles), then produces the single
        work/review/publish/merge gate report (ADR-0029).

        The existing :meth:`evaluate` / ``DependencyReport.runnable`` path is
        unchanged; this method is the new owner surface for stack-aware policy.

        Args:
            predecessor_facts: Git/PR facts keyed by predecessor issue number,
                gathered by the caller. Stack edges with no facts stay blocked.
            same_stack_members: Predecessor issue numbers the caller has verified
                form an explicitly discoverable, valid same-stack chain with this
                issue. Only these may relax ADR-0009 milestone scoping, and only
                for stack edges (ADR-0029 §4).
            approval_current: Whether this slice's reviewed commit is still its
                head. False re-blocks only the merge gate.
            configured_base_branch: An issue-specific base-branch rule, if any.
            dependency_graph: Optional issue->predecessors graph used to detect
                multi-issue cycles. Self-dependencies are detected without it.
        """
        edges = parse_dependency_edges(issue_body)
        deps = [
            self._evaluate_edge(edge, source_milestone, issue_number, same_stack_members)
            for edge in edges
        ]
        deps = self._annotate_structural_problems(deps, issue_number, dependency_graph)

        return build_gate_report(
            issue_number,
            deps,
            predecessor_facts,
            approval_current=approval_current,
            configured_base_branch=configured_base_branch,
        )

    def _evaluate_edge(
        self,
        edge: ParsedDependencyRef,
        source_milestone: str | None,
        source_issue_number: int,
        same_stack_members: frozenset[int],
    ) -> Dependency:
        """Resolve and state-check a single typed edge, carrying its mode."""
        if edge.problem is not None:
            # Malformed at parse time (e.g. a Stack-after: with no valid ref).
            return Dependency(
                issue_number=edge.issue_number,
                external_id=edge.external_id,
                repository=edge.repository,
                mode=edge.mode,
                state=DependencyState.UNKNOWN,
                problem=edge.problem,
                error=f"Malformed dependency reference: {edge.source_text!r}",
            )

        issue_number, external_id, repo = edge.issue_number, edge.external_id, edge.repository

        if external_id and issue_number is None:
            result = self._resolve_external_id(external_id)
            if result.error:
                assert result.dependency is not None
                return replace(result.dependency, mode=edge.mode)
            issue_number = result.issue_number

        if issue_number is None:
            return Dependency(
                issue_number=None, external_id=external_id, repository=repo,
                mode=edge.mode, state=DependencyState.UNKNOWN,
                error="No issue number to check",
            )

        # Self-dependency is a degenerate cycle; flag it without a graph.
        if issue_number == source_issue_number and repo is None:
            return Dependency(
                issue_number=issue_number, external_id=external_id, repository=repo,
                mode=edge.mode, state=DependencyState.UNKNOWN,
                problem=EdgeProblem.SELF_DEPENDENCY,
                error="Issue declares a dependency on itself",
            )

        if source_milestone is None:
            return Dependency(
                issue_number=issue_number, external_id=external_id, repository=repo,
                mode=edge.mode, state=DependencyState.CROSS_MILESTONE,
                error="Source issue has no milestone but declares dependencies",
            )

        return self._check_edge_state(
            issue_number, external_id, repo, source_milestone, edge, same_stack_members
        )

    def _check_edge_state(
        self,
        issue_number: int,
        external_id: str | None,
        repo: str | None,
        source_milestone: str,
        edge: ParsedDependencyRef,
        same_stack_members: frozenset[int],
    ) -> Dependency:
        """State-check a resolved edge with mode-aware milestone scoping."""
        try:
            state = self.issue_checker.get_issue_state(issue_number, repo)
            dep_milestone = self.issue_checker.get_issue_milestone(issue_number, repo)

            if state is None:
                return Dependency(
                    issue_number=issue_number, external_id=external_id, repository=repo,
                    mode=edge.mode, state=DependencyState.MISSING,
                    error="Issue not found or inaccessible", milestone=dep_milestone,
                )

            milestone_error = self._check_milestone_scope_for_edge(
                dep_milestone, source_milestone, edge, issue_number, same_stack_members
            )
            if milestone_error:
                return Dependency(
                    issue_number=issue_number, external_id=external_id, repository=repo,
                    mode=edge.mode, state=DependencyState.CROSS_MILESTONE,
                    error=milestone_error, milestone=dep_milestone,
                )

            dep_state = (
                DependencyState.SATISFIED
                if state.lower() == "closed"
                else DependencyState.UNSATISFIED
            )
            return Dependency(
                issue_number=issue_number, external_id=external_id, repository=repo,
                mode=edge.mode, state=dep_state, milestone=dep_milestone,
            )

        except Exception as e:
            logger.debug(
                "Error checking dependency edge %s: %s",
                edge.external_id or f"#{issue_number}", e,
            )
            return Dependency(
                issue_number=issue_number, external_id=external_id, repository=repo,
                mode=edge.mode, state=DependencyState.UNKNOWN, error=str(e),
            )

    def _check_milestone_scope_for_edge(
        self,
        dep_milestone: str | None,
        source_milestone: str,
        edge: ParsedDependencyRef,
        issue_number: int,
        same_stack_members: frozenset[int],
    ) -> str | None:
        """Milestone scope check with the bounded same-stack exception.

        Normal edges keep ADR-0009 scoping exactly. A stack edge may span
        milestones only when the caller has verified the chain is explicitly
        discoverable and valid (the predecessor is in ``same_stack_members``).
        """
        base_error = self._check_milestone_scope(dep_milestone, source_milestone)
        if base_error is None:
            return None
        if edge.mode == DependencyMode.STACK and issue_number in same_stack_members:
            return None
        return base_error

    def _annotate_structural_problems(
        self,
        deps: list[Dependency],
        source_issue_number: int,
        dependency_graph: Mapping[int, Sequence[int]] | None,
    ) -> list[Dependency]:
        """Annotate duplicate/mode-conflict and (optionally) cycle problems."""
        deps = self._annotate_duplicates(deps)
        if dependency_graph:
            deps = self._annotate_cycles(deps, source_issue_number, dependency_graph)
        return deps

    @staticmethod
    def _annotate_duplicates(deps: list[Dependency]) -> list[Dependency]:
        """Flag repeated targets as duplicates, or mode conflicts if modes differ."""
        groups: dict[tuple[str, int | str], list[int]] = defaultdict(list)
        for index, dep in enumerate(deps):
            if dep.problem is not None:
                continue
            if dep.issue_number is not None:
                key: tuple[str, int | str] = (dep.repository or "", dep.issue_number)
            elif dep.external_id:
                key = ("ext", dep.external_id.upper())
            else:
                continue
            groups[key].append(index)

        annotated = list(deps)
        for indices in groups.values():
            if len(indices) < 2:
                continue
            modes = {annotated[i].mode for i in indices}
            if len(modes) > 1:
                for i in indices:
                    annotated[i] = replace(
                        annotated[i],
                        problem=EdgeProblem.MODE_CONFLICT,
                        error="Same dependency declared with conflicting normal/stack modes",
                    )
            else:
                for i in indices[1:]:
                    annotated[i] = replace(
                        annotated[i],
                        problem=EdgeProblem.DUPLICATE_DECLARATION,
                        error="Duplicate dependency declaration",
                    )
        return annotated

    @staticmethod
    def _annotate_cycles(
        deps: list[Dependency],
        source_issue_number: int,
        dependency_graph: Mapping[int, Sequence[int]],
    ) -> list[Dependency]:
        """Flag edges whose target shares a cycle with the source issue."""
        cyclic = detect_cycles(dependency_graph)
        if source_issue_number not in cyclic:
            return deps
        return [
            replace(
                dep,
                problem=EdgeProblem.CYCLE,
                error="Edge participates in a dependency cycle",
            )
            if dep.problem is None and dep.issue_number in cyclic
            else dep
            for dep in deps
        ]

    def _check_all_dependencies(
        self,
        dep_refs: list[ParsedDependencyRef],
        source_milestone: str | None,
        issue_number: int,
    ) -> dict[str, list[Dependency]]:
        """Check all dependencies and categorize them."""
        result = {"satisfied": [], "unsatisfied": [], "missing": [], "unknown": [], "cross_milestone": []}

        if source_milestone is None:
            # Source has no milestone - all deps are cross-milestone
            for ref in dep_refs:
                dep = Dependency(
                    issue_number=ref.issue_number, external_id=ref.external_id, repository=ref.repository,
                    state=DependencyState.CROSS_MILESTONE, error="Source issue has no milestone but declares dependencies",
                )
                result["cross_milestone"].append(dep)
                logger.warning("Issue #%d has no milestone but declares dependency %s", issue_number, dep.display_ref)
            return result

        state_to_key = {
            DependencyState.SATISFIED: "satisfied",
            DependencyState.UNSATISFIED: "unsatisfied",
            DependencyState.MISSING: "missing",
            DependencyState.CROSS_MILESTONE: "cross_milestone",
        }

        for ref in dep_refs:
            dep = self._check_dependency_ref(ref, source_milestone)
            key = state_to_key.get(dep.state, "unknown")
            result[key].append(dep)

        return result

    def _log_dependency_warnings(self, report: DependencyReport, issue_number: int) -> None:
        """Log warnings for problematic dependencies."""
        if not report.has_warnings:
            return
        for dep in report.missing:
            logger.warning("Issue #%d has missing dependency %s: %s", issue_number, dep.display_ref, dep.error)
        for dep in report.unknown:
            logger.warning("Issue #%d has unknown dependency %s: %s", issue_number, dep.display_ref, dep.error)
        for dep in report.cross_milestone:
            logger.warning("Issue #%d has cross-milestone dependency %s: %s", issue_number, dep.display_ref, dep.error)

    def _check_dependency_ref(
        self,
        ref: ParsedDependencyRef,
        source_milestone: str,
    ) -> Dependency:
        """Check the state of a parsed dependency reference."""
        issue_number, external_id, repo = ref.issue_number, ref.external_id, ref.repository

        # Resolve external ID if needed
        if external_id and issue_number is None:
            result = self._resolve_external_id(external_id)
            if result.error:
                assert result.dependency is not None  # error=True implies dependency is set
                return result.dependency
            issue_number = result.issue_number

        if issue_number is None:
            return Dependency(issue_number=None, external_id=external_id, repository=repo, state=DependencyState.UNKNOWN, error="No issue number to check")

        return self._check_issue_state(issue_number, external_id, repo, source_milestone, ref)

    class _ExternalIdResult:
        """Result of resolving an external ID."""
        def __init__(self, issue_number: int | None = None, error: bool = False, dependency: "Dependency | None" = None):
            self.issue_number = issue_number
            self.error = error
            self.dependency = dependency

    def _resolve_external_id(self, external_id: str) -> "_ExternalIdResult":
        """Resolve external ID to issue number."""
        if self.issue_resolver is None or self.repo is None:
            logger.warning("External ID dependency %s cannot be resolved - no resolver configured", external_id)
            return self._ExternalIdResult(error=True, dependency=Dependency(issue_number=None, external_id=external_id, state=DependencyState.UNKNOWN, error="No resolver configured for external ID references"))

        key = GitHubIssueKey(repo=self.repo, external_id=external_id)
        try:
            handle = self.issue_resolver.resolve(key)
        except RepositoryHostError as e:
            # Infrastructure failure (transport, 4xx/5xx from search API, etc.)
            # is NOT the same as "issue doesn't exist." Classify as UNKNOWN so
            # the dep stays unresolved rather than getting stamped MISSING and
            # blocking the downstream issue indefinitely. Programming errors
            # (TypeError, AttributeError) intentionally propagate.
            logger.warning(
                "External ID %s could not be queried: %s", external_id, e,
            )
            return self._ExternalIdResult(error=True, dependency=Dependency(
                issue_number=None, external_id=external_id,
                state=DependencyState.UNKNOWN,
                error=f"Resolver query failed for {external_id}: {e}",
            ))

        if handle is None:
            return self._ExternalIdResult(error=True, dependency=Dependency(issue_number=None, external_id=external_id, state=DependencyState.MISSING, error=f"Could not resolve external ID {external_id} to issue number"))

        if isinstance(handle, int):
            return self._ExternalIdResult(issue_number=handle)

        return self._ExternalIdResult(error=True, dependency=Dependency(issue_number=None, external_id=external_id, state=DependencyState.UNKNOWN, error=f"Resolver returned non-int handle: {type(handle).__name__}"))

    def _check_issue_state(
        self,
        issue_number: int,
        external_id: str | None,
        repo: str | None,
        source_milestone: str,
        ref: ParsedDependencyRef,
    ) -> Dependency:
        """Check the state of a dependency issue."""
        try:
            state = self.issue_checker.get_issue_state(issue_number, repo)
            dep_milestone = self.issue_checker.get_issue_milestone(issue_number, repo)

            if state is None:
                return Dependency(issue_number=issue_number, external_id=external_id, repository=repo, state=DependencyState.MISSING, error="Issue not found or inaccessible", milestone=dep_milestone)

            # Check milestone scope
            milestone_error = self._check_milestone_scope(dep_milestone, source_milestone)
            if milestone_error:
                return Dependency(issue_number=issue_number, external_id=external_id, repository=repo, state=DependencyState.CROSS_MILESTONE, error=milestone_error, milestone=dep_milestone)

            # Check open/closed state
            dep_state = DependencyState.SATISFIED if state.lower() == "closed" else DependencyState.UNSATISFIED
            return Dependency(issue_number=issue_number, external_id=external_id, repository=repo, state=dep_state, milestone=dep_milestone)

        except Exception as e:
            logger.debug("Error checking dependency %s: %s", ref.external_id or f"#{issue_number}", e)
            return Dependency(issue_number=issue_number, external_id=external_id, repository=repo, state=DependencyState.UNKNOWN, error=str(e))

    def _check_milestone_scope(self, dep_milestone: str | None, source_milestone: str) -> str | None:
        """Check if dependency is in valid milestone scope. Returns error message or None."""
        if dep_milestone is None:
            return f"Dependency has no milestone (source is in {source_milestone})"
        if dep_milestone != source_milestone and dep_milestone != self.foundation_milestone:
            return f"Dependency in {dep_milestone}, not in {source_milestone} or {self.foundation_milestone}"
        return None

    def _emit_event(self, report: DependencyReport) -> None:
        """Emit trace event for dependency evaluation."""
        self.events.publish(
            make_trace_event(
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
