"""Project the dependency gate report onto the public UI contract.

This is the single producer-to-contract boundary for stack dependency gates
(#6597, ADR-0029). The producer (scheduler / dependency evaluator) builds a
:class:`DependencyGateReport`; this module turns that report — plus the inverted
successor edges — into the :class:`StackDependencyGateView` public contract the
dashboard and issue detail render. The UI never recomputes dependency policy;
it reads the projected view.

Pure projection: no I/O, no policy decisions. Every gate decision, reason code,
and stale determination comes straight from the report.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..contracts.public import (
    StackChipView,
    StackDependencyGateView,
    StackGateStatusView,
    StackPredecessorEdgeView,
    StackSuccessorEdgeView,
)
from ..domain.dependencies import DependencyMode
from ..domain.dependency_gates import (
    ApprovalFreshness,
    DependencyGateReport,
    DependencyGateSnapshot,
    Gate,
    GateBlockReason,
    SuccessorEdge,
)

# Ordered gates so the view always lists work → review → publish → merge.
_GATE_ORDER: tuple[Gate, ...] = (Gate.WORK, Gate.REVIEW, Gate.PUBLISH, Gate.MERGE)

# Reason codes that mean the slice itself is stale (as opposed to merely
# ordered behind an open predecessor). ``PREDECESSOR_BRANCH_ADVANCED`` marks a
# successor whose branch no longer contains the current predecessor head;
# ``APPROVAL_STALE`` marks a slice whose reviewed commit is no longer its head.
_STALE_REASONS: frozenset[GateBlockReason] = frozenset(
    {
        GateBlockReason.PREDECESSOR_BRANCH_ADVANCED,
        GateBlockReason.APPROVAL_STALE,
    }
)

# Human phrasing for each machine-readable reason code. Kept beside the codes so
# the drawer renders readable text while the UI still branches on the code.
_REASON_PHRASE: dict[GateBlockReason, str] = {
    GateBlockReason.DEPENDENCY_OPEN: "waiting on an open dependency",
    GateBlockReason.DEPENDENCY_MISSING: "a dependency could not be found",
    GateBlockReason.DEPENDENCY_UNKNOWN: "a dependency state is temporarily unknown",
    GateBlockReason.CROSS_MILESTONE: "a dependency is outside this milestone",
    GateBlockReason.SELF_DEPENDENCY: "the issue depends on itself",
    GateBlockReason.DUPLICATE_DECLARATION: "a dependency is declared more than once",
    GateBlockReason.MODE_CONFLICT: "a dependency is declared both normal and stacked",
    GateBlockReason.MALFORMED_REFERENCE: "a dependency reference is malformed",
    GateBlockReason.CYCLE: "the dependency graph has a cycle",
    GateBlockReason.BASE_BRANCH_CONFLICT: "the configured base branch conflicts with the predecessor",
    GateBlockReason.AMBIGUOUS_STACK_BASE: "more than one unmerged predecessor exposes a base branch",
    GateBlockReason.PREDECESSOR_BRANCH_UNUSABLE: "the predecessor branch is not a usable base yet",
    GateBlockReason.PREDECESSOR_VALIDATION_PENDING: "the predecessor branch has not passed validation",
    GateBlockReason.PREDECESSOR_REVIEW_PENDING: "the predecessor branch has not passed agent review",
    GateBlockReason.PREDECESSOR_NOT_MERGED: "the predecessor has not merged yet",
    GateBlockReason.PREDECESSOR_BRANCH_ADVANCED: "the predecessor branch advanced and this slice is stale",
    GateBlockReason.APPROVAL_STALE: "the reviewed commit is no longer this slice's head",
}


def reason_phrase(reason: GateBlockReason) -> str:
    """Human-readable phrase for a gate block reason code."""
    return _REASON_PHRASE.get(reason, reason.value.replace("_", " "))


def _classify_mode(
    report: DependencyGateReport | None,
    successors: Sequence[SuccessorEdge],
) -> tuple[str, bool]:
    """Overall dependency mode for the issue and whether it participates in a stack.

    ``mode`` reflects the issue's own predecessor edges (none/normal/stack).
    ``has_stack_edges`` is broader: it is true when the issue either has a stack
    predecessor edge *or* is the base of a stack successor, so a stack base with
    no dependencies of its own still surfaces chain context.
    """
    own_stack = report is not None and any(
        dep.mode == DependencyMode.STACK for dep in report.dependencies
    )
    successor_stack = any(edge.mode == DependencyMode.STACK for edge in successors)
    has_stack_edges = own_stack or successor_stack
    if own_stack:
        return "stack", has_stack_edges
    if report is not None and report.dependencies:
        return "normal", has_stack_edges
    return "none", has_stack_edges


def _gate_views(report: DependencyGateReport | None) -> list[StackGateStatusView]:
    if report is None:
        return []
    views: list[StackGateStatusView] = []
    for gate in _GATE_ORDER:
        decision = report.gate(gate)
        codes = decision.reason_codes
        views.append(
            StackGateStatusView(
                gate=gate.value,
                open=decision.is_open,
                reason_codes=[code.value for code in codes],
                reasons=[reason_phrase(code) for code in codes],
            )
        )
    return views


def _distinct_reason_codes(report: DependencyGateReport | None) -> list[str]:
    """Distinct blocked reason codes across all gates, in gate/gate-block order."""
    if report is None:
        return []
    seen: list[str] = []
    for gate in _GATE_ORDER:
        for code in report.gate(gate).reason_codes:
            if code.value not in seen:
                seen.append(code.value)
    return seen


def _stale_state(report: DependencyGateReport | None) -> tuple[bool, list[str]]:
    if report is None:
        return (False, [])
    codes: list[str] = []
    for gate in _GATE_ORDER:
        for code in report.gate(gate).reason_codes:
            if code in _STALE_REASONS and code.value not in codes:
                codes.append(code.value)
    return (bool(codes), codes)


def project_stack_dependency_view(
    issue_number: int,
    report: DependencyGateReport | None,
    successors: Sequence[SuccessorEdge] = (),
) -> StackDependencyGateView:
    """Project a gate report (+ successor edges) onto the public contract.

    ``report`` may be ``None`` for an issue that is the base of a stack — it has
    no dependency edges of its own but does have successors stacked behind it —
    so chain context still renders.
    """
    mode, has_stack_edges = _classify_mode(report, successors)
    stale, stale_reason_codes = _stale_state(report)
    predecessors = (
        [
            StackPredecessorEdgeView(
                ref=dep.display_ref,
                mode=dep.mode.value,
                state=dep.state.value,
                problem=dep.problem.value if dep.problem is not None else None,
            )
            for dep in report.dependencies
        ]
        if report is not None
        else []
    )
    return StackDependencyGateView(
        issue_number=issue_number,
        mode=mode,
        has_stack_edges=has_stack_edges,
        gates=_gate_views(report),
        predecessors=predecessors,
        successors=[
            StackSuccessorEdgeView(
                issue_number=edge.issue_number,
                ref=edge.ref,
                mode=edge.mode.value,
            )
            for edge in successors
        ],
        blocked_gates=(
            [gate.value for gate in report.blocked_gates()] if report is not None else []
        ),
        blocked_reason_codes=_distinct_reason_codes(report),
        stale=stale,
        stale_reason_codes=stale_reason_codes,
        stack_base_branch=report.stack_base_branch if report is not None else None,
        approval_freshness=(
            report.approval_freshness.value
            if report is not None
            else ApprovalFreshness.UNKNOWN.value
        ),
    )


def project_from_snapshot(
    snapshot: DependencyGateSnapshot,
    issue_number: int,
) -> StackDependencyGateView | None:
    """Project the stored snapshot for one issue, or ``None`` when there is no
    dependency/stack context to show (no report and no successors)."""
    report = snapshot.report_for(issue_number)
    successors = snapshot.successors_for(issue_number)
    if report is None and not successors:
        return None
    return project_stack_dependency_view(issue_number, report, successors)


def stack_dependency_view(state, issue_number: int) -> StackDependencyGateView | None:
    """Producer-provided stack gate view for an issue, or ``None`` when absent.

    Reads the gate snapshot the control layer stored on ``state`` and projects
    it onto the public contract. The UI renders this directly; it never
    re-derives dependency policy from issue bodies. Production ``state`` always
    carries a real snapshot (a dataclass default); the type check only skips
    projection when ``state`` is not a real orchestrator state (e.g. a test
    double), where there is no producer snapshot to render.
    """
    snapshot = getattr(state, "dependency_gate_snapshot", None)
    if not isinstance(snapshot, DependencyGateSnapshot):
        return None
    return project_from_snapshot(snapshot, issue_number)


def stack_dependency_payload(view: StackDependencyGateView | None) -> Any:
    """Serialize a stack gate view for embedding in a card / detail payload."""
    return view.model_dump(mode="json") if view is not None else None


def stack_chip(view: StackDependencyGateView | None) -> StackChipView | None:
    """Precompute the compact stack-chip display, or ``None`` when no chip shows.

    The single owner of the chip's mode label / tone / status text / title. Both
    the server template (first paint) and the client rebuild render from these
    fields, so the display logic is not duplicated between Jinja and JS — the
    divergence that let a first-paint stacked card match the refreshed fingerprint
    yet lack the chip DOM (#6597). Status is conveyed by text (not colour only):
    ``ready`` / ``stale`` / ``"<gate> [+N] blocked"``.
    """
    if view is None or not view.has_stack_edges:
        return None
    mode_label = (
        "Stack" if view.mode == "stack" else ("Deps" if view.predecessors else "Base")
    )
    tone, status_text = "ok", "ready"
    if view.stale:
        tone, status_text = "stale", "stale"
    elif view.blocked_gates:
        tone = "blocked"
        extra = f" +{len(view.blocked_gates) - 1}" if len(view.blocked_gates) > 1 else ""
        status_text = f"{view.blocked_gates[0]}{extra} blocked"
    detail_parts: list[str] = []
    if view.predecessors:
        detail_parts.append("after " + ", ".join(e.ref for e in view.predecessors))
    if view.successors:
        detail_parts.append("before " + ", ".join(e.ref for e in view.successors))
    title = f"{mode_label}: {status_text}"
    if detail_parts:
        title += " — " + "; ".join(detail_parts)
    return StackChipView(
        tone=tone, mode_label=mode_label, status_text=status_text, title=title
    )


def stack_chip_payload(chip: StackChipView | None) -> Any:
    """Serialize a precomputed stack chip for embedding in a card payload."""
    return chip.model_dump(mode="json") if chip is not None else None


def stack_signal(view: StackDependencyGateView | None) -> str:
    """Compact, fingerprint-safe encoding of a card's visible stack gate state.

    Empty when the issue does not participate in a stack. Otherwise it encodes
    *every* input ``renderStackChipHtml`` reads to render the compact chip: the
    dependency mode, the ordered blocked gates, staleness, and the predecessor
    and successor refs shown in the chip's mode label and hover/title chain
    context. The refs (not just a successor *count*) are included because the
    chip's title changes when a base moves from "before #30" to "before #31" or
    a successor from "after #10" to "after #11" even though the count and gates
    are unchanged — omitting them would leave a reused card with stale chain
    text. Any change to the rendered chip therefore re-fingerprints the card.
    """
    if view is None or not view.has_stack_edges:
        return ""
    parts = [view.mode, ",".join(view.blocked_gates)]
    if view.stale:
        parts.append("stale")
    parts.append(",".join(edge.ref for edge in view.predecessors))
    parts.append(",".join(edge.ref for edge in view.successors))
    return ":".join(parts)
