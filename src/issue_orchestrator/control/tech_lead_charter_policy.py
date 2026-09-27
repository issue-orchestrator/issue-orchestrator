"""The ONE owner of "may the tech lead do this unattended?" (#7330).

Every tech-lead authority question — the per-action ``tech_lead.authority.*``
modes, the finding-promotion mode (``tech_lead.findings.promote``), and the
per-role ``tech_lead.charter`` dials — is answered here and nowhere else. The
decision planner, the promotion lane, the dedup gate and the pattern-registry
wiring all ask this object; a guardrail test fails if any other module reads
those dials directly.

It is the orchestrator's authority, applied where completions are planned: the
agent's decision artifact is intent only, and nothing in it can choose a role or
a depth (:mod:`..domain.tech_lead_charter` derives both from the action type).

The decisions it makes are persisted through
:class:`RecordTechLeadCharterDecisionsAction`, so a reader can explain an item's
state later from the record rather than by recomputing it against a charter
that may have changed since.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from ..domain.tech_lead_charter import (
    CHARTER_ACTION_CLASSES,
    PROMOTE_FINDING_KIND,
    CharterAuthority,
    CharterOutcome,
    CharterRole,
    CharterVerdict,
    TechLeadCharter,
    decide_charter,
    promotion_ceiling,
)
from ..domain.tech_lead_charter_decisions import TechLeadCharterDecision
from .action_base import Action, ActionType
from .action_results import ActionResult
from .tech_lead_mutation import NO_RECONCILIATION_SUBJECT, TechLeadMutation

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..infra.config_models import TechLeadAuthorityConfig, TechLeadFindingsConfig
    from ..ports.tech_lead_authority import TechLeadAuthorityStore


@dataclass(frozen=True)
class TechLeadCharterPolicy:
    """The charter plus the per-action ceilings it composes with."""

    charter: TechLeadCharter
    authority: "TechLeadAuthorityConfig"
    findings: "TechLeadFindingsConfig"

    @classmethod
    def from_config(cls, config: "Config") -> "TechLeadCharterPolicy":
        tech_lead = config.tech_lead
        return cls(
            charter=tech_lead.charter.to_charter(),
            authority=tech_lead.authority,
            findings=tech_lead.findings,
        )

    def _ceiling(self, kind: str) -> tuple[CharterAuthority, str]:
        if kind == PROMOTE_FINDING_KIND:
            return promotion_ceiling(self.findings.promote)
        return (
            CharterAuthority(self.authority.mode_for(kind)),
            f"tech_lead.authority.{kind}",
        )

    def decide(self, kind: str) -> CharterVerdict:
        """The charter verdict for one action kind under the current config."""
        ceiling, source = self._ceiling(kind)
        return decide_charter(
            kind, self.charter, action_ceiling=ceiling, ceiling_source=source
        )

    def executes(self, kind: str) -> bool:
        return self.decide(kind).executes

    @property
    def promotion_lane_enabled(self) -> bool:
        """``tech_lead.findings.promote: off`` switches the whole lane off."""
        return self.findings.enabled

    def promotion(self) -> CharterVerdict:
        """The verdict every finding promotion is filed under."""
        if not self.promotion_lane_enabled:
            raise ValueError("finding promotion is off; no promotion verdict exists")
        return self.decide(PROMOTE_FINDING_KIND)

    def promotion_gated(self) -> bool:
        """True when a promoted issue must carry the operator-approval gate label.

        False when the lane is off (nothing is filed), matching the pre-charter
        ``tech_lead.findings.gated``.
        """
        return self.promotion_lane_enabled and not self.promotion().executes

    def outcomes_by_role(
        self,
    ) -> dict[CharterRole, dict[CharterOutcome, tuple[CharterVerdict, ...]]]:
        """Every classified kind's verdict, grouped by role then outcome.

        The prompt projection and the board both render from this, so what the
        agent is told and what the orchestrator enforces are one computation.
        A disabled promotion lane contributes no promotion verdict.
        """
        grouped: dict[CharterRole, dict[CharterOutcome, list[CharterVerdict]]] = {
            role: {} for role in CharterRole
        }
        for kind in CHARTER_ACTION_CLASSES:
            if kind == PROMOTE_FINDING_KIND and not self.promotion_lane_enabled:
                continue
            verdict = self.decide(kind)
            grouped[verdict.role].setdefault(verdict.outcome, []).append(verdict)
        return {
            role: {outcome: tuple(items) for outcome, items in by_outcome.items()}
            for role, by_outcome in grouped.items()
        }


@dataclass(frozen=True)
class RecordTechLeadCharterDecisionsAction(Action):
    """Persist the charter decisions of one planned completion (or promotion pass).

    Writes only the orchestrator-owned charter ledger, so it names no
    reconciliation subject and carries no expected state (like the terminal
    op discard). Idempotent per decision id: a replayed completion upserts.
    """

    decisions: tuple[TechLeadCharterDecision, ...] = ()
    action_type: ActionType = field(
        default=ActionType.RECORD_TECH_LEAD_CHARTER_DECISIONS, init=False
    )

    def __post_init__(self) -> None:
        if not self.decisions:
            raise ValueError("RecordTechLeadCharterDecisionsAction needs decisions")
        ids = [decision.decision_id for decision in self.decisions]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate charter decision ids in one record: {ids}")

    def reconciliation_subject(self) -> int:
        return NO_RECONCILIATION_SUBJECT


@dataclass(frozen=True)
class CharterAuditedAction(Action):
    """An effect that runs only after its charter decision is on the record.

    Used where the effect and its record would otherwise be separate actions in
    an ungated tick (the promotion lane): a failed record write would leave a
    filed effect with no decision explaining it, and nothing would re-record it
    once the effect made the candidate ineligible (#7330 review r3 F3). The
    inner effect still goes through the applier's own dispatch and guards.
    """

    decisions: tuple[TechLeadCharterDecision, ...] = ()
    effect: Action | None = None
    action_type: ActionType = field(
        default=ActionType.APPLY_CHARTER_AUDITED_ACTION, init=False
    )

    def __post_init__(self) -> None:
        if not self.decisions or self.effect is None:
            raise ValueError("CharterAuditedAction needs decisions and an effect")
        if self.expected is not None:
            raise ValueError(
                "CharterAuditedAction takes its expected state from its effect"
            )
        # The wrapper is guarded with its EFFECT's expectations, so the applier's
        # gate refuses a drifted subject BEFORE the charter decision is written.
        # Guarded only at the effect's own dispatch, a refused effect left a
        # decision on the record for an effect that never ran (isolation review).
        object.__setattr__(self, "expected", self.effect.expected)  # effect checked above

    def reconciliation_subject(self) -> int:
        # The effect's subject: the wrapper's record is only true if the effect
        # may run, so both are gated on the same issue. The effect is guarded
        # again when it is dispatched.
        effect = self.effect
        if isinstance(effect, TechLeadMutation):
            return effect.reconciliation_subject()
        return NO_RECONCILIATION_SUBJECT


def apply_charter_audited_action(
    action: Action,
    *,
    authority: "TechLeadAuthorityStore | None",
    apply_action: "Callable[[Action], ActionResult]",
) -> ActionResult:
    assert isinstance(action, CharterAuditedAction) and action.effect is not None
    recorded = apply_record_tech_lead_charter_decisions(
        RecordTechLeadCharterDecisionsAction(decisions=action.decisions, reason=action.reason),
        authority=authority,
    )
    if not recorded.success:
        return ActionResult.fail(
            action, f"charter decision not recorded; effect withheld: {recorded.error}"
        )
    return apply_action(action.effect)


def apply_record_tech_lead_charter_decisions(
    action: Action, *, authority: "TechLeadAuthorityStore | None"
) -> ActionResult:
    assert isinstance(action, RecordTechLeadCharterDecisionsAction)
    if authority is None:
        return ActionResult.fail(
            action,
            "recording tech-lead charter decisions requires the"
            " TechLeadAuthorityStore wired into this applier",
        )
    authority.charter_ledger.record_decisions(action.decisions)
    return ActionResult.ok(action, recorded=len(action.decisions))
