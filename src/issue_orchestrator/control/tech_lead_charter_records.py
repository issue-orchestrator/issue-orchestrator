"""Charter bookkeeping for one planned tech-lead decision (#7330).

The decision planner asks :class:`~.tech_lead_charter_policy.TechLeadCharterPolicy`
for a verdict per proposed action and hands each one here. This module turns the
verdicts into the persisted :class:`TechLeadCharterDecision` records and renders
the operator text for actions the charter kept as advice, so the planner stays a
translation from verdict to action and does not grow the record format.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Sequence

from ..domain.tech_lead_artifacts import ACT_LEVEL_TECH_LEAD_ACTIONS, ProposedTechLeadAction
from ..domain.tech_lead_charter import CharterOutcome, CharterReason, CharterVerdict
from ..domain.tech_lead_charter_decisions import (
    CharterDecisionSource,
    TechLeadCharterDecision,
    decision_key,
    promotion_decision_key,
)
from ..domain.tech_lead_findings import PromotableFinding
from .action_base import Action
from .tech_lead_actions import PromoteTechLeadFindingAction
from .tech_lead_charter_policy import (
    CharterAuditedAction,
    RecordTechLeadCharterDecisionsAction,
    TechLeadCharterPolicy,
)

#: Advice produced by the per-action ``tech_lead.authority.*`` mode alone. It
#: keeps the pre-charter would-have-done wording; every other advice reason is
#: the charter's own and is explained with the charter's reason text.
_LEGACY_SHADOW_REASONS = frozenset({CharterReason.ADVISORY_ACTION_AUTHORITY_PROPOSE})


def is_charter_advice(verdict: CharterVerdict) -> bool:
    """True when the CHARTER (not the per-action mode) kept this as advice."""
    return verdict.advice_only and verdict.reason_code not in _LEGACY_SHADOW_REASONS


@dataclass
class CharterDecisionLog:
    """Verdicts of one decision, in proposal order, keyed by action id."""

    run_id: str
    anchor_issue_number: int
    decided_at: str
    _verdicts: dict[str, tuple[ProposedTechLeadAction, CharterVerdict]] = field(
        default_factory=dict
    )
    _reused_proposals: dict[str, int] = field(default_factory=dict)
    _coalesced: dict[str, str] = field(default_factory=dict)

    def discard(self) -> None:
        """Forget every verdict: the whole decision was rejected, nothing applies."""
        self._verdicts.clear()
        self._reused_proposals.clear()
        self._coalesced.clear()

    def note(self, proposed: ProposedTechLeadAction, verdict: CharterVerdict) -> None:
        self._verdicts[proposed.id] = (proposed, verdict)

    def verdict_for(self, action_id: str) -> CharterVerdict:
        return self._verdicts[action_id][1]

    def note_reused_proposal(self, action_id: str, proposal_issue_number: int) -> None:
        """A re-proposal commented onto an existing gated proposal issue."""
        self._reused_proposals[action_id] = proposal_issue_number

    def acted_on_targets(self, kind: str) -> frozenset[int]:
        """Targets of *kind* proposals the charter did NOT keep as advice.

        What a proposal effectively does is the charter's call, so a caller that
        needs "which PRs are going back for rework" reads it here rather than
        re-reading the agent's raw intent.
        """
        return frozenset(
            proposed.target_number
            for proposed, verdict in self._verdicts.values()
            if proposed.action_type == kind
            and proposed.target_number is not None
            and not verdict.advice_only
        )

    def link_effects(
        self, action_id: str, actions: list[Action], before: Sequence[Action]
    ) -> None:
        """Stamp the effects planning *action_id* added or changed with its decision.

        Only a decision the charter let EXECUTE is linked (#7362): its record
        then learns what the applier really did with those effects. An effect
        folded into an earlier one (a coalesced case file) carries both
        decisions. Edits *actions* in place, since planners hold the list.
        """
        if self.verdict_for(action_id).outcome is not CharterOutcome.EXECUTED:
            return
        key = decision_key(self.run_id, action_id)
        for index, action in enumerate(actions):
            if index < len(before) and action is before[index]:
                continue
            if key not in action.charter_decisions:
                actions[index] = replace(
                    action, charter_decisions=(*action.charter_decisions, key)
                )

    def note_coalesced(self, action_id: str, origin_action_id: str) -> None:
        """A same-(op, target) sibling folded into *origin_action_id*'s proposal."""
        self._coalesced[action_id] = origin_action_id

    def records(self) -> tuple[TechLeadCharterDecision, ...]:
        return tuple(
            TechLeadCharterDecision.from_verdict(
                verdict,
                decision_id=decision_key(self.run_id, proposed.id),
                source=CharterDecisionSource.DECISION,
                run_id=self.run_id,
                action_id=proposed.id,
                anchor_issue_number=self.anchor_issue_number,
                target_number=proposed.target_number,
                target_is_pr=proposed.target_is_pr,
                decided_at=self.decided_at,
                # Only act-level proposals are backed by the stored-op ledger
                # whose approval and decline link back to this record.
                tracks_proposal=proposed.action_type in ACT_LEVEL_TECH_LEAD_ACTIONS,
                proposal_issue_number=self._reused_proposals.get(proposed.id),
                proposal_origin_action_id=self._coalesced.get(proposed.id),
            )
            for proposed, verdict in self._verdicts.values()
        )

    def record_action(self) -> list[Action]:
        records = self.records()
        if not records:
            return []
        return [
            RecordTechLeadCharterDecisionsAction(
                decisions=records,
                reason=(
                    f"tech_lead charter: record {len(records)} decision(s) for"
                    f" run {self.run_id}"
                ),
            )
        ]


def charter_advice_digest_lines(
    items: list[tuple[str, str, int, str]],
) -> list[str]:
    """Digest lines for actions the charter kept as advice.

    Each item is ``(action_id, action_type, target_number, reason)``.
    """
    lines = [
        "",
        "### Advice only (outside the tech-lead charter)",
        "",
        "These proposals fall outside what `tech_lead.charter` lets the tech lead"
        " do, so the orchestrator recorded them for you instead of acting:",
        "",
    ]
    for action_id, action_type, target, reason in items:
        where = f"#{target}" if target else "n/a"
        lines.append(f"- **{action_id}** `{action_type}` (target: {where}) — {reason}")
    return lines


def audit_promotions(
    policy: TechLeadCharterPolicy,
    promotable: "tuple[PromotableFinding, ...] | list[PromotableFinding]",
    planned: list[Action],
    *,
    decided_at: str,
) -> list[Action]:
    """Bind each promotion filing to its charter decision (#7330).

    A planned filing becomes a :class:`CharterAuditedAction`, so it cannot run
    without its decision on the record. A candidate the charter kept as advice
    files nothing; its standalone record is what explains why no promotion
    issue appears (upserted, so an unchanged verdict is not re-dated).
    """
    if not promotable or not policy.promotion_lane_enabled:
        return planned
    verdict = policy.promotion()
    decisions = {
        finding.evidence.signature: TechLeadCharterDecision.from_verdict(
            verdict,
            decision_id=promotion_decision_key(finding.evidence.signature),
            source=CharterDecisionSource.PROMOTION,
            run_id="",
            action_id=finding.evidence.signature,
            anchor_issue_number=finding.evidence.case_file_issue_number,
            target_number=finding.evidence.case_file_issue_number,
            target_is_pr=False,
            decided_at=decided_at,
            tracks_proposal=False,
        )
        for finding in promotable
    }
    audited: list[Action] = []
    filed: set[str] = set()
    for action in planned:
        if isinstance(action, PromoteTechLeadFindingAction):
            filed.add(action.signature)
            audited.append(CharterAuditedAction(
                decisions=(decisions[action.signature],), effect=action,
                reason=action.reason,
            ))
        else:
            audited.append(action)
    advice = tuple(d for signature, d in decisions.items() if signature not in filed)
    if advice:
        audited.append(RecordTechLeadCharterDecisionsAction(
            decisions=advice,
            reason=f"tech_lead charter: record {len(advice)} unfiled promotion decision(s)",
        ))
    return audited


def partition_charter_records(
    actions: "Sequence[Action]",
) -> tuple[list[Action], list[Action]]:
    """Split charter-record actions (the audit) from every other action."""
    records: list[Action] = [
        a for a in actions if isinstance(a, RecordTechLeadCharterDecisionsAction)
    ]
    return records, [a for a in actions if not isinstance(a, RecordTechLeadCharterDecisionsAction)]
