"""The durable would-have-done digest for a tech-lead decision's shadow proposals.

Extracted from the decision planner (#7330): it renders one anchor comment
listing every proposal the orchestrator recorded instead of executing, split by
WHY — the per-action ``tech_lead.authority.*`` mode, or the tech-lead charter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .actions import AddCommentAction, SurfaceTechLeadProposalAction
from .tech_lead_charter_records import (
    CharterDecisionLog,
    charter_advice_digest_lines,
    is_charter_advice,
)

if TYPE_CHECKING:
    from .reconciliation import ExpectedState


def shadow_digest_comment(
    shadow: list[SurfaceTechLeadProposalAction],
    *,
    anchor_issue_number: int,
    expected: "ExpectedState",
    charter_log: CharterDecisionLog,
) -> AddCommentAction:
    """Durable would-have-done record for shadow proposals (#6761 finding 6).

    Trace events are ephemeral; the operator-facing escalation surface is the
    crash-safe GitHub comment/label channel. One digest comment per completion
    keeps the record bounded while listing every proposal the configured
    authority did not execute.

    Proposals kept as advice by the per-action ``tech_lead.authority.*`` mode
    keep their pre-charter wording (so default-config output is unchanged);
    proposals the charter kept as advice get their own section, each with the
    charter's reason (#7330).
    """
    advice = [
        item for item in shadow
        if is_charter_advice(charter_log.verdict_for(item.action_id))
    ]
    legacy = [item for item in shadow if item not in advice]
    lines: list[str] = []
    if legacy:
        lines.extend(_legacy_shadow_lines(legacy))
    else:
        lines.append("## 🔍 Tech Lead proposals recorded, not executed")
    if advice:
        lines.extend(charter_advice_digest_lines([
            (item.action_id, item.proposal_type, item.target_number,
             charter_log.verdict_for(item.action_id).reason)
            for item in advice
        ]))
    return AddCommentAction(
        number=anchor_issue_number,
        comment="\n".join(lines),
        is_pr=False,
        reason="tech_lead decision: durable shadow-proposal record (would-have-done)",
        expected=expected,
    )


def _legacy_shadow_lines(shadow: list[SurfaceTechLeadProposalAction]) -> list[str]:
    """Would-have-done lines for proposals whose per-action mode is propose."""
    lines = [
        "## 🔍 Tech Lead proposals recorded, not executed (shadow mode)",
        "",
        "The tech_lead decision proposed the following actions. Configured"
        " authority is `propose` for them, so the orchestrator recorded"
        " them as *would-have-done* instead of executing (ADR-0031):",
        "",
    ]
    for item in shadow:
        target = f"#{item.target_number}" if item.target_number else "n/a"
        title = f" — {item.title}" if item.title else ""
        lines.append(
            f"- **{item.action_id}** `{item.proposal_type}` (target: {target}){title}"
        )
        if item.body_preview:
            lines.append(f"  > {item.body_preview}")
        if item.finding_ids:
            lines.append(f"  findings: {', '.join(item.finding_ids)}")
    # Only immediate/report-tier types reach the shadow digest (#6778):
    # create_issue proposals become gated issues, and act-level proposals
    # become gated proposal issues — the anchor gets a per-proposal link
    # comment from the creation applier instead of a digest entry. Every
    # remaining shadow type is a real, flip-able authority knob.
    knob_types = sorted({item.proposal_type for item in shadow})
    lines.append("")
    knobs = ", ".join(f"`tech_lead.authority.{name}`" for name in knob_types)
    lines.append(
        f"*Flip {knobs} to `execute` to let the orchestrator perform these next time.*"
    )
    return lines
