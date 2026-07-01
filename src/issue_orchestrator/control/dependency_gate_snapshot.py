"""Build the per-tick dependency gate snapshot from scheduler decisions.

The scheduler already evaluates a :class:`DependencyGateReport` for every issue
whose dependencies it checks (#6597, ADR-0029). This module is the single owner
that turns those decisions — plus the inverted predecessor graph — into a
:class:`DependencyGateSnapshot` stored on ``OrchestratorState`` for the UI to
project. It performs no I/O: reports come from the decisions, and successor
edges are parsed from issue bodies the caller already holds.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..domain.dependencies import parse_dependency_edges
from ..domain.dependency_gates import (
    DependencyGateReport,
    DependencyGateSnapshot,
    SuccessorEdge,
)
from ..ports.issue import Issue
from .scheduler import IssueAvailabilityDecision


def _collect_reports(
    decisions: Sequence[IssueAvailabilityDecision],
) -> dict[int, DependencyGateReport]:
    """Retain gate reports that carry real dependency context.

    Only reports with at least one dependency edge are kept: an issue with no
    declared dependencies has an all-open, edge-less report that would add noise
    (and never renders a stack section) if stored.
    """
    reports: dict[int, DependencyGateReport] = {}
    for decision in decisions:
        report = decision.gate_report
        if report is not None and report.dependencies:
            reports[decision.issue.number] = report
    return reports


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


def build_dependency_gate_snapshot(
    decisions: Sequence[IssueAvailabilityDecision],
    issues: Sequence[Issue],
) -> DependencyGateSnapshot:
    """Assemble the dependency gate snapshot for the current in-scope set."""
    return DependencyGateSnapshot(
        reports=_collect_reports(decisions),
        successors=_build_successor_index(issues),
    )
