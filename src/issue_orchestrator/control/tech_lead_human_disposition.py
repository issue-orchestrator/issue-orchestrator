"""Compose an explicit human disposition through the shared lifecycle owner."""
from __future__ import annotations
from typing import TYPE_CHECKING, Callable
from .action_base import Action
from .action_results import ActionResult
from .tech_lead_actions import EscalateTechLeadDispositionAction
from .tech_lead_needs_human_reconcile import (
    TechLeadNeedsHumanLifecycle, discover_tech_lead_needs_human_issue_numbers,
)
if TYPE_CHECKING:
    from ..ports import RepositoryHost, EventSink
    from .needs_human_block import SharedNeedsHumanBlock
    from .label_manager import LabelManager


def apply_human_disposition(action: Action, *, host: "RepositoryHost | None",
    labels: "LabelManager | None", events: "EventSink",
    apply_action: Callable[[Action], ActionResult],
    needs_human_block: "SharedNeedsHumanBlock") -> ActionResult:
    assert isinstance(action, EscalateTechLeadDispositionAction)
    if host is None or labels is None:
        return ActionResult.fail(action, "human disposition requires repository host and label policy")
    owner = TechLeadNeedsHumanLifecycle(
        labels=labels, events=events, read_labels=host.get_issue_labels_fresh,
        discover_marked_issue_numbers=lambda: discover_tech_lead_needs_human_issue_numbers(host, labels.tech_lead_needs_human),
        apply_actions=lambda actions, context: all(apply_action(item).success for item in actions),
        needs_human_block=needs_human_block,
    )
    if not owner.escalate(issue_number=action.issue_number, reason=action.reason,
        preserve_existing_human=True, comment=action.comment, context="tech-lead disposition",
        event_data={"issue_number": action.issue_number, "reason": action.reason}):
        return ActionResult.fail(action, "human disposition did not commit")
    return ActionResult.ok(action, issue_number=action.issue_number)
