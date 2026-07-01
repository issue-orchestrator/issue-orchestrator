"""Build the per-tick dependency gate snapshot for the dashboard.

The dashboard and issue detail render stack state from a producer-evaluated
:class:`DependencyGateSnapshot` (#6597, ADR-0029). This module is the single
owner that produces that snapshot, and it sits at the **dependency-gate policy
boundary**: it evaluates each in-scope issue's full four-gate report directly
through the :class:`DependencyEvaluator`, rather than reusing the scheduler's
*availability* decisions.

That distinction matters. The scheduler short-circuits before dependency
evaluation for ``pr-pending`` / blocked / in-progress states, so a stack
successor awaiting merge — the very lane where the merge gate is most relevant —
would have no report at all. The scheduler's work gate also cannot represent the
publish-time ancestry fact or the merge-time approval fact. Evaluating through
the gate owner here means every relevant lane surfaces its gates, and the
publish/merge-specific facts the UI shows (a stale successor branch, a stale
own-approval) are folded in where the caller can supply them — from an active
successor worktree for ancestry, and from a per-issue approval-freshness map for
merge.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.dependencies import parse_dependency_edges
from ..domain.dependency_gates import (
    DependencyGateReport,
    DependencyGateSnapshot,
    SuccessorEdge,
)
from ..ports.issue import Issue

if TYPE_CHECKING:
    from ..domain.models import Session
    from .dependency_evaluator import DependencyEvaluator


def _build_successor_index(
    issues: Sequence[Issue],
) -> dict[int, tuple[SuccessorEdge, ...]]:
    """Invert same-repo predecessor edges into a predecessor→successors map.

    For each issue that declares an edge onto a resolvable same-repo issue, the
    target gains a successor edge naming the declaring issue and the edge mode.
    Cross-repo and unresolved (external-ID) edges are skipped — successor chain
    context is best-effort and same-repo only.
    """
    successors: dict[int, list[SuccessorEdge]] = {}
    seen: dict[int, set[int]] = {}
    for issue in issues:
        if not issue.body:
            continue
        for edge in parse_dependency_edges(issue.body):
            if edge.issue_number is None or edge.repository is not None:
                continue
            target = edge.issue_number
            if target == issue.number:
                continue
            claimed = seen.setdefault(target, set())
            if issue.number in claimed:
                continue
            claimed.add(issue.number)
            successors.setdefault(target, []).append(
                SuccessorEdge(
                    issue_number=issue.number,
                    ref=f"#{issue.number}",
                    mode=edge.mode,
                )
            )
    return {
        target: tuple(sorted(edges, key=lambda e: e.issue_number))
        for target, edges in successors.items()
    }


class DependencyGateSnapshotBuilder:
    """Owner that projects dependency gate state for the UI snapshot.

    Holds the :class:`DependencyEvaluator` (the four-gate policy owner) and turns
    the in-scope issue set into a :class:`DependencyGateSnapshot`. Report
    evaluation runs through :meth:`DependencyEvaluator.evaluate_all_gates`, which
    performs no availability short-circuit and emits no event, so every lane —
    including short-circuited ``pr-pending`` / blocked successors — surfaces its
    gates.

    The GitHub reads underlying stack-predecessor facts are served from the label
    and PR caches the same refresh cycle just populated, so evaluating here does
    not add fresh GitHub I/O for the common case.
    """

    def __init__(self, evaluator: "DependencyEvaluator | None") -> None:
        self._evaluator = evaluator

    def build(
        self,
        issues: Sequence[Issue],
        *,
        worktrees_by_issue: Mapping[int, Path] | None = None,
        approval_current_by_issue: Mapping[int, bool] | None = None,
    ) -> DependencyGateSnapshot:
        """Assemble the dependency gate snapshot for the current in-scope set.

        Args:
            issues: Every in-scope issue this refresh cycle observed.
            worktrees_by_issue: Active successor worktrees keyed by issue number.
                A worktree lets the publish gate run its ancestry check, so a
                stale successor branch surfaces ``predecessor_branch_advanced``.
                An issue with no active worktree keeps ancestry at its contained
                default — the same conservative stance the live publish gate
                takes when it has no working copy.
            approval_current_by_issue: Per-issue reviewed-commit freshness. A
                ``False`` entry re-blocks that issue's merge gate with
                ``approval_stale``. Issues absent from the map default to fresh.
        """
        worktrees = worktrees_by_issue or {}
        approvals = approval_current_by_issue or {}
        reports: dict[int, DependencyGateReport] = {}
        if self._evaluator is not None:
            for issue in issues:
                if not issue.body:
                    continue
                report = self._evaluator.evaluate_all_gates(
                    issue.number,
                    issue.body,
                    issue.milestone,
                    worktree=worktrees.get(issue.number),
                    approval_current=approvals.get(issue.number, True),
                )
                # An issue with no declared edges yields an all-open, edge-less
                # report that never renders a stack section — skip it so the
                # snapshot only carries reports with real dependency context.
                if report.dependencies:
                    reports[issue.number] = report
        return DependencyGateSnapshot(
            reports=reports,
            successors=_build_successor_index(issues),
        )


def build_refresh_snapshot(
    evaluator: "DependencyEvaluator | None",
    issues: Sequence[Issue],
    active_sessions: Sequence["Session"],
) -> DependencyGateSnapshot:
    """Build the dependency gate snapshot for one queue-refresh tick.

    The refresh loop's composition helper: it derives each active successor's
    worktree from ``active_sessions`` (so the publish gate can run its ancestry
    check) and delegates to :class:`DependencyGateSnapshotBuilder`. Keeping the
    session→worktree mapping here means the refresh loop does not need to know
    how the snapshot owner sources its ancestry facts.
    """
    worktrees_by_issue = {
        session.issue.number: session.worktree_path for session in active_sessions
    }
    return DependencyGateSnapshotBuilder(evaluator).build(
        issues, worktrees_by_issue=worktrees_by_issue
    )
