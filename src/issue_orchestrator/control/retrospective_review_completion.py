"""Completion action mapping for retrospective-review sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..domain.models import (
    Session,
    SessionStatus,
    resolve_retrospective_coder_agent,
)
from ..domain.session_key import TaskKind
from .actions import (
    Action,
    AddCommentAction,
    AddLabelAction,
    RemoveLabelAction,
    SetIssueStateAction,
)

if TYPE_CHECKING:
    from ..infra.config import Config
    from .label_manager import LabelManager


def retrospective_review_completion_actions(
    *,
    session: Session,
    status: SessionStatus,
    detail: dict[str, Any],
    config: "Config",
    label_manager: "LabelManager",
) -> tuple[Action, ...]:
    """Return label/state actions for retrospective review outcomes.

    Rework queueing for changes-requested is handled after these actions in
    session_completion._queue_rework_after_retrospective_changes.
    """

    if session.key.task != TaskKind.RETROSPECTIVE_REVIEW:
        return ()
    if status != SessionStatus.COMPLETED:
        return ()

    outcome = str(detail.get("outcome") or "")
    issue_number = session.issue.number
    if outcome == "review_approved":
        return (
            RemoveLabelAction(
                issue_number=issue_number,
                label=config.retrospective_review_trigger_label,
                reason="retrospective review approved - clearing trigger",
            ),
            RemoveLabelAction(
                issue_number=issue_number,
                label=config.retrospective_changes_requested_label,
                reason="retrospective review approved - clearing stale changes request",
            ),
            AddLabelAction(
                issue_number=issue_number,
                label=config.retrospective_reviewed_label,
                reason="retrospective review approved existing implementation",
            ),
        )
    if outcome == "review_changes_requested":
        coder_agent = resolve_retrospective_coder_agent(session.issue, session.agent_label)
        if coder_agent is None:
            return _retrospective_review_needs_human_actions(
                issue_number=issue_number,
                config=config,
                label_manager=label_manager,
            )
        return (
            RemoveLabelAction(
                issue_number=issue_number,
                label=config.retrospective_review_trigger_label,
                reason="retrospective review requested changes - clearing trigger",
            ),
            RemoveLabelAction(
                issue_number=issue_number,
                label=config.retrospective_reviewed_label,
                reason="retrospective review requested changes - clearing reviewed marker",
            ),
            RemoveLabelAction(
                issue_number=issue_number,
                label=label_manager.needs_rework,
                reason=(
                    "retrospective review requested changes - clearing generic "
                    "review rework marker"
                ),
            ),
            AddLabelAction(
                issue_number=issue_number,
                label=config.retrospective_changes_requested_label,
                reason="retrospective review requested coder rework",
            ),
            SetIssueStateAction(
                issue_number=issue_number,
                state="open",
                reason="retrospective review requested coder rework",
            ),
        )
    return ()


def _retrospective_review_needs_human_actions(
    *,
    issue_number: int,
    config: "Config",
    label_manager: "LabelManager",
) -> tuple[Action, ...]:
    reason = (
        "retrospective review requested changes but no coder agent label "
        "was available"
    )
    return (
        RemoveLabelAction(
            issue_number=issue_number,
            label=config.retrospective_review_trigger_label,
            reason=f"{reason} - clearing trigger",
        ),
        RemoveLabelAction(
            issue_number=issue_number,
            label=config.retrospective_reviewed_label,
            reason=f"{reason} - clearing reviewed marker",
        ),
        RemoveLabelAction(
            issue_number=issue_number,
            label=label_manager.needs_rework,
            reason=f"{reason} - clearing generic review rework marker",
        ),
        AddLabelAction(
            issue_number=issue_number,
            label=config.retrospective_changes_requested_label,
            reason=reason,
        ),
        AddLabelAction(
            issue_number=issue_number,
            label=label_manager.needs_human,
            reason=reason,
        ),
        AddCommentAction(
            number=issue_number,
            comment=(
                "## Retrospective Review Needs Human\n\n"
                "The retrospective reviewer requested changes, but the "
                "orchestrator could not resolve a coder `agent:*` label "
                "for this issue. Add the correct coder agent label and "
                f"remove `{label_manager.needs_human}` to continue."
            ),
            reason=reason,
        ),
        SetIssueStateAction(
            issue_number=issue_number,
            state="open",
            reason=reason,
        ),
    )
