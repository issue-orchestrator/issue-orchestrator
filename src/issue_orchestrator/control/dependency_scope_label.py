"""The ONE owner of the derived ``blocked-cross-milestone`` label (#7333).

The label is a projection of the dependency work gate: an issue carries it
exactly while its work gate is blocked by an ADR-0009 milestone-scope
violation. Adding it used to live inline in the planner, keyed on the text of
the scheduler's skip detail, and nothing ever took it off. Because the label
is itself blocking, the scheduler stopped evaluating the issue's dependencies
the tick after adding it; the dependency problem then looked resolved
(``dependency.unblocked``) while the label parked the issue for good, and the
stuck sweep escalated the leftover label to a human (porchpin#326).

So the label is reconciled here, in both directions, from the gate's own typed
verdict:

* an issue the scheduler found dependency-blocked by a scope violation gets the
  label when it lacks it;
* an issue that carries the label is re-evaluated by the gate (the label
  stopped the scheduler before it got that far), and loses the label when the
  gate no longer reports a scope violation.

Only issues already carrying the label pay for the extra evaluation.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..domain.dependencies import DependencyState
from .actions import Action, AddLabelAction, RemoveLabelAction
from .reconciliation import build_expected_for_mutation

if TYPE_CHECKING:
    from .dependency_evaluator import DependencyEvaluator
    from .label_manager import LabelManager
    from .scheduler import IssueAvailabilityDecision


def plan_dependency_scope_labels(
    decisions: Sequence["IssueAvailabilityDecision"],
    *,
    label_manager: "LabelManager",
    evaluator: "DependencyEvaluator",
) -> list[Action]:
    """Add or remove ``blocked-cross-milestone`` so it matches the work gate."""
    label = label_manager.blocked_cross_milestone
    folded = label.casefold()
    actions: list[Action] = []
    for decision in decisions:
        issue = decision.issue
        if issue.state == "closed":
            continue
        carries = any(name.casefold() == folded for name in issue.labels)
        if not carries:
            if decision.is_dependency_blocked and decision.violates_milestone_scope:
                actions.append(AddLabelAction(
                    issue_number=issue.number,
                    label=label,
                    reason=f"dependency violates milestone scope: {decision.detail}",
                    expected=build_expected_for_mutation(),
                    issue_key=issue.key.stable_id(),
                ))
            continue
        if issue.body:
            report = evaluator.evaluate_work_gate(
                issue_number=issue.number,
                issue_body=issue.body,
                source_milestone=issue.milestone,
                emit_event=False,
            )
            if report.work_violates_milestone_scope or any(
                dependency.state is DependencyState.UNKNOWN
                for dependency in report.dependencies
            ):
                # Still violated, or a dependency could not be read: an
                # undecided gate keeps the label rather than flapping it on a
                # transient lookup failure.
                continue
        actions.append(RemoveLabelAction(
            issue_number=issue.number,
            label=label,
            reason="dependency gate no longer reports a milestone-scope violation",
            expected=build_expected_for_mutation(),
            issue_key=issue.key.stable_id(),
        ))
    return actions
