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
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..domain.dependencies import (
    Dependency,
    DependencyMode,
    DependencyReport,
    DependencyState,
    DependencyTarget,
    EdgeProblem,
    ParsedDependencyRef,
    parse_dependency_edges,
    parse_dependency_refs,
)
from ..domain.dependency_gates import (
    DependencyGateReport,
    Gate,
    PredecessorFacts,
    build_gate_report,
    detect_cycles,
)
from ..domain.issue_key import GitHubIssueKey
from ..events import EventName
from ..ports import EventSink, IssueResolver, make_trace_event
from ..ports.repository_host import DependencyIssueSnapshot, RepositoryHostError
from ..ports.stack_branch_ancestry import StackBranchAncestry
from ..ports.stack_predecessor_facts import StackPredecessorFactsProvider

logger = logging.getLogger(__name__)


@runtime_checkable
class IssueStateChecker(Protocol):
    """Protocol for checking dependency issue state and milestone."""

    def get_dependency_issue_snapshot(
        self,
        issue_number: int,
        repo: str | None = None,
    ) -> DependencyIssueSnapshot | None:
        """Get dependency issue facts, or None if not found."""
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
        predecessor_facts_provider: StackPredecessorFactsProvider | None = None,
        branch_ancestry: StackBranchAncestry | None = None,
    ):
        """Initialize the evaluator.

        Args:
            issue_checker: Adapter to check issue states and milestones
            events: EventSink for trace events
            issue_resolver: Optional resolver for external ID references
            repo: Repository in owner/repo format (required if issue_resolver is set)
            foundation_milestone: Milestone that any issue can depend on (default: M0)
            predecessor_facts_provider: Gathers git/PR facts for stack
                predecessors (ADR-0029). Required for stack edges to ever
                unblock work; when absent, stack edges stay conservatively
                blocked. Normal ``Depends-on:`` edges never consult it.
            branch_ancestry: Decides whether a successor working copy still
                contains its stack predecessor's branch head (#6596). Consulted
                only by :meth:`evaluate_publish_gate` (which has a worktree).
                When absent, ancestry defaults to "contained" so existing
                publish behavior is preserved.
        """
        self.issue_checker = issue_checker
        self.events = events
        self.issue_resolver = issue_resolver
        self.repo = repo
        self.foundation_milestone = foundation_milestone
        self._predecessor_facts_provider = predecessor_facts_provider
        self._branch_ancestry = branch_ancestry

    def attach_branch_ancestry(self, branch_ancestry: StackBranchAncestry) -> None:
        """Wire the stack branch-ancestry checker after construction.

        The composition root builds the ancestry checker (which needs a
        command runner created later than this evaluator) and attaches it here,
        so the same single owner the work gate uses also resolves publish-time
        staleness. Until attached, ancestry defaults to "contained".
        """
        self._branch_ancestry = branch_ancestry

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
        predecessor_facts: Mapping[DependencyTarget, PredecessorFacts] | None = None,
        same_stack_members: frozenset[DependencyTarget] = frozenset(),
        approval_current: bool | None = True,
        configured_base_branch: str | None = None,
        dependency_graph: Mapping[DependencyTarget, Sequence[DependencyTarget]] | None = None,
    ) -> DependencyGateReport:
        """Evaluate typed dependency edges into a dependency gate report.

        Parses both ``Depends-on:`` (normal) and ``Stack-after:`` (stack) edges,
        resolves and checks each, annotates structural problems (self-dependency,
        duplicates, mode conflicts, cycles), then produces the single
        work/review/publish/merge gate report (ADR-0029).

        The existing :meth:`evaluate` / ``DependencyReport.runnable`` path is
        unchanged; this method is the new owner surface for stack-aware policy.

        Every target-keyed input uses the repository-aware
        :class:`DependencyTarget` so a same-repo and a cross-repo predecessor
        with the same issue number stay distinct across the whole gate boundary
        (facts, membership, and cycle graph).

        Args:
            predecessor_facts: Git/PR facts keyed by the predecessor's
                :class:`DependencyTarget`, gathered by the caller. Stack edges
                with no facts stay blocked.
            same_stack_members: Predecessor :class:`DependencyTarget`s the caller
                has verified form an explicitly discoverable, valid same-stack
                chain with this issue. Only these may relax ADR-0009 milestone
                scoping, and only for stack edges (ADR-0029 §4).
            approval_current: Whether this slice's reviewed commit is still its
                head. False re-blocks only the merge gate.
            configured_base_branch: An issue-specific base-branch rule, if any.
            dependency_graph: Optional target->predecessors graph used to detect
                multi-issue cycles. Self-dependencies are detected without it.
        """
        deps = self._resolve_edges(
            issue_number, issue_body, source_milestone, same_stack_members, dependency_graph
        )

        return build_gate_report(
            issue_number,
            deps,
            predecessor_facts,
            approval_current=approval_current,
            configured_base_branch=configured_base_branch,
        )

    def evaluate_all_gates(
        self,
        issue_number: int,
        issue_body: str,
        source_milestone: str | None = None,
        *,
        worktree: Path | None = None,
        approval_current: bool | None = True,
        configured_base_branch: str | None = None,
    ) -> DependencyGateReport:
        """Build the full work/review/publish/merge report from every fact.

        This is the single owner that composes *all* inputs the four gates read:
        the issue's resolved edges, stack-predecessor git/PR facts, publish-time
        branch ancestry (when a successor ``worktree`` is available), and the
        slice's own merge-time ``approval_current`` freshness. The gate-specific
        methods (:meth:`evaluate_work_gate`, :meth:`evaluate_publish_gate`,
        :meth:`evaluate_merge_gate`) and the dashboard snapshot owner all route
        through here, so no caller re-derives stack policy or drops a fact class.

        It performs no availability short-circuit and emits no event, so it can
        project the gate state of *any* lane — including a ``pr-pending`` /
        awaiting-merge successor the scheduler never reaches. Facts unknowable in
        the caller's context keep their conservative :class:`PredecessorFacts`
        default (ancestry with no worktree stays "contained"; ``approval_current``
        defaults fresh) rather than being invented, as the live gates behave.
        """
        deps = self._resolve_edges(
            issue_number, issue_body, source_milestone, frozenset(), None
        )
        facts = self._gather_predecessor_facts(deps)
        facts = self._refine_ancestry(facts, worktree)
        return build_gate_report(
            issue_number,
            deps,
            facts,
            approval_current=approval_current,
            configured_base_branch=configured_base_branch,
        )

    def evaluate_work_gate(
        self,
        issue_number: int,
        issue_body: str,
        source_milestone: str | None = None,
        *,
        configured_base_branch: str | None = None,
        emit_event: bool = True,
    ) -> DependencyGateReport:
        """Build the dependency gate report, gathering stack-predecessor facts.

        This is the single owner the scheduler, session launch, and planner
        consume to decide whether an issue's *work* may start (ADR-0029 §3). It
        composes the report through :meth:`evaluate_all_gates` (with no worktree,
        so ancestry stays contained, and a fresh approval) and reads
        ``report.can_start_work`` (and the machine-readable
        ``gate_block_records(Gate.WORK)``) rather than re-deriving stack policy.

        For an issue with only normal ``Depends-on:`` edges this matches the
        legacy ``DependencyReport.runnable`` decision (open iff every dependency
        is closed); stack edges also need a usable, validated, reviewed branch.

        Args:
            configured_base_branch: An issue-specific base-branch rule, if any. A
                stack base that conflicts with it surfaces as a validation
                failure rather than being silently resolved.
            emit_event: Whether to publish the ``dependencies.evaluated`` trace
                event. Diagnostics that only need the decision can suppress it.
        """
        report = self.evaluate_all_gates(
            issue_number,
            issue_body,
            source_milestone,
            configured_base_branch=configured_base_branch,
        )
        if emit_event and report.dependencies:
            self._emit_work_gate_event(report)
        return report

    def evaluate_publish_gate(
        self,
        issue_number: int,
        issue_body: str,
        source_milestone: str | None = None,
        *,
        worktree: Path | None = None,
        configured_base_branch: str | None = None,
    ) -> DependencyGateReport:
        """Build the gate report for a *publish* decision (ADR-0029 §3, #6596).

        Callers read ``report.can_publish`` (and ``report.stack_base_branch`` for
        the PR base). This is :meth:`evaluate_all_gates` with the successor
        ``worktree`` wired in, so a predecessor that advanced without the
        successor containing it re-blocks publish with
        ``PREDECESSOR_BRANCH_ADVANCED``; a non-stack issue collapses to "publish
        open unless a normal ``Depends-on:`` is still open", exactly as before.
        """
        return self.evaluate_all_gates(
            issue_number,
            issue_body,
            source_milestone,
            worktree=worktree,
            configured_base_branch=configured_base_branch,
        )

    def evaluate_merge_gate(
        self,
        issue_number: int,
        issue_body: str,
        source_milestone: str | None = None,
        *,
        approval_current: bool | None = True,
        configured_base_branch: str | None = None,
    ) -> DependencyGateReport:
        """Build the gate report for an ordered *merge* decision (#6596).

        Merge stays strictly ordered: a stack successor's merge gate is blocked
        (``PREDECESSOR_NOT_MERGED``) until its predecessor merges/closes, and the
        slice's own stale approval re-blocks merge (``APPROVAL_STALE``). This is
        :meth:`evaluate_all_gates` with *no* worktree — merge intentionally does
        not run the successor-vs-predecessor ancestry check (publish-time
        ancestry, then GitHub's own mergeable state, already guard the stale-base
        case). A non-stack issue collapses to the legacy closed-only rule.
        """
        return self.evaluate_all_gates(
            issue_number,
            issue_body,
            source_milestone,
            approval_current=approval_current,
            configured_base_branch=configured_base_branch,
        )

    def _refine_ancestry(
        self,
        facts: Mapping[DependencyTarget, PredecessorFacts],
        worktree: Path | None,
    ) -> Mapping[DependencyTarget, PredecessorFacts]:
        """Fold a successor-vs-predecessor ancestry check into gathered facts.

        For each stack predecessor with a usable branch, ask the injected
        :class:`StackBranchAncestry` whether the successor working copy still
        contains that branch's head; record the answer as
        ``contained_in_successor``. Without an ancestry checker or a worktree the
        facts are returned unchanged (ancestry defaults to contained), so a
        deployment that has not wired ancestry keeps prior publish behavior.
        """
        if self._branch_ancestry is None or worktree is None or not facts:
            return facts
        refined: dict[DependencyTarget, PredecessorFacts] = {}
        for target, fact in facts.items():
            if fact.branch_usable and fact.branch_name:
                contained = self._branch_ancestry.successor_contains_predecessor(
                    worktree, fact.branch_name
                )
                refined[target] = replace(fact, contained_in_successor=contained)
            else:
                refined[target] = fact
        return refined

    def _resolve_edges(
        self,
        issue_number: int,
        issue_body: str,
        source_milestone: str | None,
        same_stack_members: frozenset[DependencyTarget],
        dependency_graph: Mapping[DependencyTarget, Sequence[DependencyTarget]] | None,
    ) -> list[Dependency]:
        """Parse, resolve, state-check, and annotate the issue's typed edges."""
        edges = parse_dependency_edges(issue_body)
        deps = [
            self._evaluate_edge(edge, source_milestone, issue_number, same_stack_members)
            for edge in edges
        ]
        return self._annotate_structural_problems(deps, issue_number, dependency_graph)

    def _gather_predecessor_facts(
        self, deps: Sequence[Dependency]
    ) -> Mapping[DependencyTarget, PredecessorFacts]:
        """Gather facts for the unsatisfied stack predecessors among ``deps``.

        Only stack edges that are not already SATISFIED (closed/merged) and carry
        no structural problem need facts; a satisfied stack edge fully opens the
        gate without any I/O. Normal edges never consult the provider. With no
        provider configured this returns an empty mapping, leaving stack edges
        conservatively blocked.
        """
        if self._predecessor_facts_provider is None:
            return {}
        targets = [
            dep.target
            for dep in deps
            if dep.mode == DependencyMode.STACK
            and dep.problem is None
            and dep.state == DependencyState.UNSATISFIED
            and dep.target is not None
        ]
        if not targets:
            return {}
        return self._predecessor_facts_provider.gather_facts(targets)

    def _emit_work_gate_event(self, report: DependencyGateReport) -> None:
        """Emit the work-gate evaluation as a ``dependencies.evaluated`` event.

        Keeps the legacy :meth:`evaluate` count fields (``satisfied_count`` …
        ``cross_milestone_count``) so this catalogued event stays a stable,
        additive contract: the scheduler now emits through the work gate, and
        existing ``dependencies.evaluated`` consumers must keep reading the same
        payload shape. The work-gate-specific fields are added on top.
        """
        counts = report.dependency_state_counts()
        self.events.publish(
            make_trace_event(
                EventName.DEPENDENCIES_EVALUATED,
                {
                    "issue_number": report.issue_number,
                    "runnable": report.can_start_work,
                    "satisfied_count": counts[DependencyState.SATISFIED],
                    "unsatisfied_count": counts[DependencyState.UNSATISFIED],
                    "missing_count": counts[DependencyState.MISSING],
                    "unknown_count": counts[DependencyState.UNKNOWN],
                    "cross_milestone_count": counts[DependencyState.CROSS_MILESTONE],
                    "gate": Gate.WORK.value,
                    "blocked_gates": [g.value for g in report.blocked_gates()],
                    "blocked_reasons": [
                        record.as_dict()
                        for record in report.gate_block_records(Gate.WORK)
                    ],
                    "summary": report.work_summary(),
                },
            )
        )

    def _evaluate_edge(
        self,
        edge: ParsedDependencyRef,
        source_milestone: str | None,
        source_issue_number: int,
        same_stack_members: frozenset[DependencyTarget],
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
        same_stack_members: frozenset[DependencyTarget],
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
        same_stack_members: frozenset[DependencyTarget],
    ) -> str | None:
        """Milestone scope check with the bounded same-stack exception.

        Normal edges keep ADR-0009 scoping exactly. A stack edge may span
        milestones only when the caller has verified the chain is explicitly
        discoverable and valid (the predecessor's repository-aware target is in
        ``same_stack_members``). Matching on the typed target — not the bare
        issue number — keeps a cross-repo predecessor from borrowing the
        same-stack exception granted to a same-number local member.
        """
        base_error = self._check_milestone_scope(dep_milestone, source_milestone)
        if base_error is None:
            return None
        target = DependencyTarget(issue_number=issue_number, repository=edge.repository)
        if edge.mode == DependencyMode.STACK and target in same_stack_members:
            return None
        return base_error

    def _annotate_structural_problems(
        self,
        deps: list[Dependency],
        source_issue_number: int,
        dependency_graph: Mapping[DependencyTarget, Sequence[DependencyTarget]] | None,
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
        dependency_graph: Mapping[DependencyTarget, Sequence[DependencyTarget]],
    ) -> list[Dependency]:
        """Flag edges whose target shares a cycle with the source issue.

        Cycle membership is decided on repository-aware targets: the source
        issue is the local target ``#<source>``, and an edge is only flagged
        when its own :attr:`Dependency.target` is in the cyclic set. A
        cross-repo target that happens to share a number with a cyclic local
        node is therefore not falsely implicated.
        """
        cyclic = detect_cycles(dependency_graph)
        source_target = DependencyTarget(issue_number=source_issue_number)
        if source_target not in cyclic:
            return deps
        return [
            replace(
                dep,
                problem=EdgeProblem.CYCLE,
                error="Edge participates in a dependency cycle",
            )
            if dep.problem is None and dep.target is not None and dep.target in cyclic
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
            snapshot = self.issue_checker.get_dependency_issue_snapshot(issue_number, repo)

            if snapshot is None:
                return Dependency(issue_number=issue_number, external_id=external_id, repository=repo, state=DependencyState.MISSING, error="Issue not found or inaccessible")

            # Check milestone scope
            milestone_error = self._check_milestone_scope(snapshot.milestone, source_milestone)
            if milestone_error:
                return Dependency(issue_number=issue_number, external_id=external_id, repository=repo, state=DependencyState.CROSS_MILESTONE, error=milestone_error, milestone=snapshot.milestone)

            # Check open/closed state
            dep_state = DependencyState.SATISFIED if snapshot.state.lower() == "closed" else DependencyState.UNSATISFIED
            return Dependency(issue_number=issue_number, external_id=external_id, repository=repo, state=dep_state, milestone=snapshot.milestone)

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
