"""Concrete decision lowering after tech-lead authority and dedup policy."""
from __future__ import annotations
from typing import TYPE_CHECKING
from ..domain.tech_lead_artifacts import ProposedTechLeadAction
from ..domain.tech_lead_comment import TechLeadCommentIntent, provenance_footer
from ..domain.tech_lead_session import TechLeadDisposition, TechLeadCreationOrigin
from ..ports.issue import Issue
from .actions import Action, CreateTechLeadIssueAction, RecordTechLeadDispositionAction, EscalateTechLeadDispositionAction
from .required_issue_comment import TechLeadDecisionCommentAction
from .label_manager import LabelManager
from .tech_lead_issue_policy import (apply_tech_lead_priority_prefix, decision_issue_labels,
    tech_lead_follow_up_agent_label, tech_lead_issue_milestone_intent)
if TYPE_CHECKING:
    from ..infra.config import Config
    from .reconciliation import ExpectedState


def concrete_tech_lead_actions(
    action: ProposedTechLeadAction,
    *,
    config: "Config",
    labels: LabelManager,
    anchor_issue: Issue,
    expected: "ExpectedState",
    source_run_id: str,
    source_session_name: str,
    observed_at: str,
    gate_reason: str | None = None,
) -> list[Action]:
    body = (action.body or "") + provenance_footer(action.id, action.finding_ids)
    if action.action_type == "post_comment":
        assert action.target_number is not None  # enforced by validate()
        return [
            TechLeadDecisionCommentAction(
                intent=TechLeadCommentIntent.from_action(action),
                number=action.target_number,
                comment=body,
                is_pr=action.target_is_pr,
                reason=f"tech_lead decision action {action.id}: post diagnosis comment",
                expected=expected,
            )
        ]
    if action.action_type == "create_issue":
        # Config policy (tech_lead: labels/priority/milestone strategy) is the
        # single tech_lead_issue_policy owner, shared with the planner's batch
        # tracking issue; agent labels passed the protected-set contract
        # check at decision validation time (#6761 finding 4). The milestone
        # travels as INTENT — name resolution happens in the applier at
        # creation time, so planning makes zero GitHub reads (#6769 F4).
        anchor_milestones = (
            [(anchor_issue.milestone_number, anchor_issue.milestone or "")]
            if anchor_issue.milestone_number is not None
            else []
        )
        # A gate reason both gates the issue and explains itself in the operator-
        # facing body — never a bare boolean whose meaning callers must guess.
        gated = gate_reason is not None
        gated_body = f"{body}\n\n---\n> {gate_reason}" if gated else body
        return [
            CreateTechLeadIssueAction(
                title=apply_tech_lead_priority_prefix(config, action.title or ""),
                body=gated_body,
                labels=decision_issue_labels(
                    config,
                    anchor_labels=anchor_issue.labels,
                    agent_labels=action.labels,
                    labels=labels,
                    # Orchestrator-owned routing label so removing the gate
                    # alone lands a schedulable issue (#6779 R5); attached for
                    # execute-authority create_issue too — both need an agent.
                    destination_agent=tech_lead_follow_up_agent_label(config),
                    gate=gated,
                    area=action.area,
                ),
                pr_count=0,
                # DECIDED by a session working this anchor — so the anchor's
                # pause label gates the creation, and the ExpectedState below is
                # what the gate checks (#6957 F3/A3, R2 F6/A6).
                origin=TechLeadCreationOrigin.derived_from_anchor(anchor_issue.number),
                milestone=tech_lead_issue_milestone_intent(config, anchor_milestones),
                # Expedite intent (#6870) rides the action so the applier's
                # create boundary can front-queue the new issue. It composes
                # with the gate: gated (propose) creations defer expediting to
                # gate removal, ungated (execute) creations expedite at once.
                expedite=action.expedite,
                reason=(
                    f"tech_lead decision action {action.id}: create follow-up"
                    f" issue{' (gated)' if gated else ''}"
                ),
                expected=expected,
            )
        ]
    if action.action_type == "defer_to_tracker":
        assert action.target_number is not None  # enforced by validate()
        assert action.tracker_number is not None  # enforced by validate()
        # One owned command publishes and commits this terminal outcome.
        disposition = TechLeadDisposition(
            issue_number=action.target_number,
            tracker_issue_number=action.tracker_number,
            rationale=body,
            source_run_id=source_run_id,
            source_session_name=source_session_name,
            source_action_id=action.id,
            recorded_at=observed_at,
            finding_ids=action.finding_ids,
        )
        return [
            RecordTechLeadDispositionAction(
                disposition=disposition,
                reason=(
                    f"tech_lead decision action {action.id}: park #{action.target_number}"
                    f" on recovery tracker #{action.tracker_number}"
                ),
                expected=expected,
            ),
        ]
    if action.action_type == "escalate_to_human":
        assert action.target_number is not None  # enforced by validate()
        return [EscalateTechLeadDispositionAction(
            issue_number=action.target_number,
            comment="## Tech Lead escalation — human attention needed\n\n" + body,
            reason=f"tech_lead decision action {action.id}: escalate to human",
            expected=expected,
        )]

    raise ValueError(
        f"no concrete executor for tech_lead action type {action.action_type!r}"
    )

