"""Map a validated triage decision to orchestrator actions (ADR-0031).

Pure policy: no IO, no events. The triage agent proposed actions as intent;
this module applies the configured graduated authority per action type and
emits the orchestrator's action vocabulary:

- ``execute`` authority -> the concrete action (comment, issue, escalation).
- ``propose`` authority -> a shadow :class:`SurfaceTriageProposalAction`.
- ``flag_pattern`` with ``execute`` authority -> surfaced with
  ``mode="pattern"``: recording the pattern IS the execution. Under
  ``propose`` authority it is a shadow record like any other proposal.
- ``reset_retry`` with ``execute`` authority -> a typed
  :class:`ResetRetryIssueAction`; the applier's owner re-validates the
  proposal's preconditions at execution time and downgrades stale proposals
  to a surfaced record (#6764, ADR-0031 §2). Under ``propose`` authority it
  stays a shadow record.
- Still-unwired act-level proposals (``kill_hung_session``) -> always
  surfaced (``mode="shadow"``); config validation guarantees their authority
  is ``propose`` until the executors are wired (#6764).

Shadow proposals additionally plan ONE durable would-have-done digest
comment on the triage session's anchor issue. The trace event alone is not
an operator surface: ADR-0031 §2 requires shadow records to be visible "in
the report, as a structured event, and on the escalation surface", and the
escalation surface in this codebase is the crash-safe GitHub label/comment
channel that the dashboard projects (#6761 finding 6).

Decision-driven ``create_issue`` proposals are composed by the
``triage_issue_policy`` owner: configured triage labels/priority/milestone
strategy apply exactly as they do to the planner's batch tracking issue, and
agent labels have already passed the protected-label contract check
(``triage_completion``).

Escalation note: triage escalation deliberately does NOT reuse
``EscalateToHumanAction``. That action's applier terminates the target
issue's runtime ("escalation kills issue automation, full stop"), which
would let the always-execute ``escalate_to_human`` floor reach the same
effect as ``kill_hung_session`` — an act-level intent that is
shadow-mode-only until #6764. Triage escalation is strictly a routing
surface: needs-human label + explanatory comment on the target; nothing
is stopped and no other labels are touched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.triage_artifacts import (
    UNWIRED_ACT_LEVEL_TRIAGE_ACTIONS,
    ProposedTriageAction,
    TriageDecision,
)
from ..ports.issue import Issue
from .actions import (
    Action,
    AddCommentAction,
    AddLabelAction,
    CreateTriageIssueAction,
    ResetRetryIssueAction,
    SurfaceTriageProposalAction,
)
from .label_manager import LabelManager
from .triage_issue_policy import (
    apply_triage_priority_prefix,
    decision_issue_labels,
    triage_issue_milestone_intent,
)

if TYPE_CHECKING:
    from ..infra.config import Config
    from .reconciliation import ExpectedState

# Cap applied to SurfaceTriageProposalAction.body_preview at construction.
_BODY_PREVIEW_CHARS = 500


def _provenance_footer(action: ProposedTriageAction) -> str:
    finding_ids = ", ".join(action.finding_ids) or "none"
    return (
        f"\n\n---\n*Proposed by triage session (action {action.id};"
        f" findings: {finding_ids}) — ADR-0031.*"
    )


def _surface(
    action: ProposedTriageAction,
    *,
    anchor_issue_number: int,
    mode: str,
) -> SurfaceTriageProposalAction:
    return SurfaceTriageProposalAction(
        issue_number=anchor_issue_number,
        action_id=action.id,
        proposal_type=action.action_type,
        target_number=action.target_number or 0,
        target_is_pr=action.target_is_pr,
        title=action.title or "",
        body_preview=(action.body or "")[:_BODY_PREVIEW_CHARS],
        finding_ids=action.finding_ids,
        mode=mode,
        reason=f"triage proposal {action.id} ({action.action_type}) surfaced as {mode}",
    )


def _concrete_actions(
    action: ProposedTriageAction,
    *,
    config: "Config",
    labels: LabelManager,
    anchor_issue: Issue,
    expected: "ExpectedState",
    needs_human_label: str,
) -> list[Action]:
    body = (action.body or "") + _provenance_footer(action)
    if action.action_type == "post_comment":
        assert action.target_number is not None  # enforced by validate()
        return [
            AddCommentAction(
                number=action.target_number,
                comment=body,
                is_pr=action.target_is_pr,
                reason=f"triage decision action {action.id}: post diagnosis comment",
                expected=expected,
            )
        ]
    if action.action_type == "create_issue":
        # Config policy (triage: labels/priority/milestone strategy) is the
        # single triage_issue_policy owner, shared with the planner's batch
        # tracking issue; agent labels passed the protected-set contract
        # check at decision validation time (#6761 finding 4). The milestone
        # travels as INTENT — name resolution happens in the applier at
        # creation time, so planning makes zero GitHub reads (#6769 F4).
        anchor_milestones = (
            [(anchor_issue.milestone_number, anchor_issue.milestone or "")]
            if anchor_issue.milestone_number is not None
            else []
        )
        return [
            CreateTriageIssueAction(
                title=apply_triage_priority_prefix(config, action.title or ""),
                body=body,
                labels=decision_issue_labels(
                    config,
                    anchor_labels=anchor_issue.labels,
                    agent_labels=action.labels,
                    labels=labels,
                ),
                pr_count=0,
                milestone=triage_issue_milestone_intent(config, anchor_milestones),
                reason=f"triage decision action {action.id}: create follow-up issue",
                expected=expected,
            )
        ]
    if action.action_type == "escalate_to_human":
        assert action.target_number is not None  # enforced by validate()
        # Routing surface only — see the module docstring for why this must
        # not reuse EscalateToHumanAction (runtime termination).
        return [
            AddLabelAction(
                issue_number=action.target_number,
                label=needs_human_label,
                reason=f"triage decision action {action.id}: escalate to human",
                expected=expected,
            ),
            AddCommentAction(
                number=action.target_number,
                comment="## ⚠️ Triage escalation — human attention needed\n\n" + body,
                is_pr=action.target_is_pr,
                reason=f"triage decision action {action.id}: escalation comment",
                expected=expected,
            ),
        ]
    raise ValueError(
        f"no concrete executor for triage action type {action.action_type!r}"
    )


def plan_triage_rejection_action(
    *, anchor_issue_number: int, failure: str, detail: str
) -> SurfaceTriageProposalAction:
    """Surface a rejected decision artifact pair (``mode="rejected"``)."""
    return SurfaceTriageProposalAction(
        issue_number=anchor_issue_number,
        proposal_type="decision",
        body_preview=detail[:_BODY_PREVIEW_CHARS],
        mode="rejected",
        reason=f"triage decision rejected ({failure})",
    )


def plan_triage_decision_actions(
    decision: TriageDecision,
    config: "Config",
    labels: LabelManager,
    *,
    anchor_issue: Issue,
    expected: "ExpectedState",
) -> list[Action]:
    """Plan orchestrator actions for a validated triage decision."""
    authority = config.triage.authority
    anchor_issue_number = anchor_issue.number
    actions: list[Action] = []
    shadow: list[SurfaceTriageProposalAction] = []

    def _surface_shadow(proposed: ProposedTriageAction) -> None:
        surfaced = _surface(
            proposed, anchor_issue_number=anchor_issue_number, mode="shadow"
        )
        shadow.append(surfaced)
        actions.append(surfaced)

    for proposed in decision.proposed_actions:
        if proposed.action_type == "flag_pattern":
            # Authority-aware (#6761 finding 5): execute records the pattern
            # (mode="pattern" IS the execution); propose is shadow mode.
            if authority.mode_for("flag_pattern") == "execute":
                actions.append(
                    _surface(
                        proposed,
                        anchor_issue_number=anchor_issue_number,
                        mode="pattern",
                    )
                )
            else:
                _surface_shadow(proposed)
            continue
        if proposed.is_act_level:
            # reset_retry is wired (#6764, first slice): execute authority
            # plans the typed action whose applier re-validates the
            # preconditions at execution time. Unwired act-level intents
            # (kill_hung_session) surface unconditionally — config validation
            # guarantees their authority is "propose", but never trust it.
            if (
                proposed.action_type == "reset_retry"
                and authority.mode_for("reset_retry") == "execute"
            ):
                assert proposed.target_number is not None  # enforced by validate()
                actions.append(
                    ResetRetryIssueAction(
                        issue_number=proposed.target_number,
                        rationale=proposed.body or "",
                        proposal_id=proposed.id,
                        finding_ids=proposed.finding_ids,
                        anchor_issue_number=anchor_issue_number,
                        reason=(
                            f"triage decision action {proposed.id}:"
                            " reset and retry from scratch"
                        ),
                        expected=expected,
                    )
                )
                continue
            _surface_shadow(proposed)
            continue
        if authority.mode_for(proposed.action_type) == "execute":
            actions.extend(
                _concrete_actions(
                    proposed,
                    config=config,
                    labels=labels,
                    anchor_issue=anchor_issue,
                    expected=expected,
                    needs_human_label=labels.needs_human,
                )
            )
        else:
            _surface_shadow(proposed)
    if shadow:
        actions.append(
            _shadow_digest_comment(
                shadow, anchor_issue_number=anchor_issue_number, expected=expected
            )
        )
    return actions


def _shadow_digest_comment(
    shadow: list[SurfaceTriageProposalAction],
    *,
    anchor_issue_number: int,
    expected: "ExpectedState",
) -> AddCommentAction:
    """Durable would-have-done record for shadow proposals (#6761 finding 6).

    Trace events are ephemeral; the operator-facing escalation surface is the
    crash-safe GitHub comment/label channel. One digest comment per completion
    keeps the record bounded while listing every proposal the configured
    authority did not execute.
    """
    lines = [
        "## 🔍 Triage proposals recorded, not executed (shadow mode)",
        "",
        "The triage decision proposed the following actions. Configured"
        " authority is `propose` for them, so the orchestrator recorded"
        " them as *would-have-done* instead of executing (ADR-0031):",
        "",
    ]
    for item in shadow:
        target = f"#{item.target_number}" if item.target_number else "n/a"
        title = f" — {item.title}" if item.title else ""
        lines.append(f"- **{item.action_id}** `{item.proposal_type}` (target: {target}){title}")
        if item.body_preview:
            lines.append(f"  > {item.body_preview}")
        if item.finding_ids:
            lines.append(f"  findings: {', '.join(item.finding_ids)}")
    # Wired proposal types (including act-level reset_retry, #6764 first
    # slice) get the config-flip guidance; only the still-unwired act-level
    # intents keep the "not wired" note so operators are never told to flip
    # a knob that startup rejects (#6761 re-review finding 5).
    gated = sorted(
        {
            item.proposal_type
            for item in shadow
            if item.proposal_type not in UNWIRED_ACT_LEVEL_TRIAGE_ACTIONS
        }
    )
    unwired = sorted(
        {
            item.proposal_type
            for item in shadow
            if item.proposal_type in UNWIRED_ACT_LEVEL_TRIAGE_ACTIONS
        }
    )
    lines.append("")
    if gated:
        knobs = ", ".join(f"`triage.authority.{name}`" for name in gated)
        lines.append(
            f"*Flip {knobs} to `execute` to let the orchestrator perform"
            " these next time.*"
        )
    if unwired:
        names = ", ".join(f"`{name}`" for name in unwired)
        lines.append(
            f"*{names}: orchestrator execution is not wired yet (#6764) —"
            " startup rejects `execute` for these until it lands, so act on"
            " them manually if warranted.*"
        )
    return AddCommentAction(
        number=anchor_issue_number,
        comment="\n".join(lines),
        is_pr=False,
        reason="triage decision: durable shadow-proposal record (would-have-done)",
        expected=expected,
    )
