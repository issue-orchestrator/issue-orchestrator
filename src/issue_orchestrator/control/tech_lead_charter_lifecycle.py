"""Link what became of a tech-lead action back to its charter decision (#7330, #7362).

The ONE owner of every link-back onto a charter decision record, so a reader
explains an item from what actually happened rather than from the verdict:

* A gated act-level proposal carries its stored op, which names the run and the
  decision action id it came from. When the operator approves it (and the op
  is applied, or found stale) or closes it unapproved, the record's proposal
  lifecycle is updated. Both hooks run where the op ledger row is consumed,
  BEFORE the row is discarded — the row is the only link from proposal issue to
  decision.
* An action the charter let execute directly records ``executed`` at decision
  time, which says only that it was ALLOWED to run (#7362). Its effects carry
  the decision (``Action.charter_decisions``, or a charter-audited wrapper's
  decisions), and whatever the applier did with them — applied, refused by its
  own re-validation, failed, withheld behind a mandated action, or parked by
  the liveness owner — is linked back here after the attempt.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from ..domain.tech_lead_charter import CharterOutcome
from ..domain.tech_lead_charter_decisions import (
    CharterExecutionLink,
    CharterExecutionResult,
    CharterProposalLifecycle,
    TechLeadCharterDecision,
)
from .action_results import ActionResult, ActionResultType

if TYPE_CHECKING:
    from ..domain.action_liveness import LivenessRow
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from ..ports.tech_lead_charter_ledger import TechLeadCharterLedger
    from .action_base import Action

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _link(
    authority: "TechLeadAuthorityStore",
    proposal_issue_number: int,
    lifecycle: CharterProposalLifecycle,
) -> int:
    op = authority.load_op(issue_number=proposal_issue_number)
    if op is None:
        return 0
    return authority.charter_ledger.link_proposal_outcome(
        run_id=op.source_run_id,
        action_id=op.source_action_id,
        proposal_issue_number=proposal_issue_number,
        lifecycle=lifecycle,
        at=_now(),
    )


def link_approved_proposal(
    authority: "TechLeadAuthorityStore", proposal_issue_number: int, *, applied: bool
) -> int:
    """The operator approved it; the op ran (``applied``) or was found stale."""
    return _link(
        authority,
        proposal_issue_number,
        CharterProposalLifecycle.APPROVED_APPLIED
        if applied
        else CharterProposalLifecycle.APPROVED_STALE,
    )


def link_declined_proposal(
    authority: "TechLeadAuthorityStore", proposal_issue_number: int
) -> int:
    """The proposal issue closed without approval."""
    return _link(authority, proposal_issue_number, CharterProposalLifecycle.DECLINED)


# -- directly executed actions (#7362) -------------------------------------------


def executed_decisions(decisions: Iterable[TechLeadCharterDecision]) -> tuple[str, ...]:
    """The ids of the decisions the charter let execute directly."""
    return tuple(
        decision.decision_id
        for decision in decisions
        if decision.outcome is CharterOutcome.EXECUTED
    )


def result_of(result: ActionResult) -> tuple[CharterExecutionResult, str | None]:
    """What one applier result means for the decision it is an effect of.

    A skip is the applier's own re-validation refusing to write (every typed
    tech-lead refusal is a skip naming its reason): it did not take effect.
    A scoped rework the applier reports as done but without an effective
    receipt for its exact target did not take effect either.
    """
    from .actions import RequestReworkAction
    from .tech_lead_completion_obligations import scoped_rework_effect_committed

    if result.result_type is ActionResultType.FAILURE:
        return CharterExecutionResult.FAILED, result.error or "the applier failed without an error"
    if result.result_type is ActionResultType.SKIPPED:
        reason = str(result.details.get("skip_reason") or "the applier skipped it")
        return CharterExecutionResult.REFUSED, reason
    if isinstance(result.action, RequestReworkAction) and not scoped_rework_effect_committed(
        result
    ):
        return CharterExecutionResult.FAILED, "the scoped rework has no effective receipt"
    return CharterExecutionResult.APPLIED, None


#: When one decision has several effects, the one that says most about why it
#: did not take effect speaks for all of them: an applier's own refusal names
#: its reason, where "withheld" only points at a sibling.
_SEVERITY = (
    CharterExecutionResult.APPLIED,
    CharterExecutionResult.WITHHELD,
    CharterExecutionResult.REFUSED,
    CharterExecutionResult.FAILED,
    CharterExecutionResult.PARKED,
)


def _worst(
    results: Sequence[tuple[CharterExecutionResult, str | None]],
) -> tuple[CharterExecutionResult, str | None]:
    return max(results, key=lambda item: _SEVERITY.index(item[0]))


def link_execution_results(
    ledger: "TechLeadCharterLedger",
    results: "dict[str, list[tuple[CharterExecutionResult, str | None]]]",
) -> None:
    """Link each decision's folded result; a decision missing from the record raises."""
    links = [
        CharterExecutionLink(decision_id, *_worst(found))
        for decision_id, found in results.items()
        if found
    ]
    if not links:
        return
    updated = ledger.link_execution_outcomes(links, at=_now())
    if updated != len(links):
        raise LookupError(
            f"linked {updated} of {len(links)} executed charter decision(s):"
            " a decision whose effects ran is missing from the record"
        )


def link_or_log(link: "Callable[[], None]", what: str) -> None:
    """Run a link-back AFTER the effect it describes; never undo or mask the effect.

    The effect has already happened (or been refused); a link that cannot be
    written leaves the record without a result, which every reader shows as
    "result not recorded" and never as taken effect. So it is logged loudly
    here rather than turned into a failure of an effect that did commit.
    """
    try:
        link()
    except Exception:
        logger.exception("[tech_lead] Could not link %s back to its charter decision", what)


@dataclass
class CompletionEffectLinks:
    """What a completion's apply did with every executed decision's effects.

    The completion owner notes each batch it applies, in order, and whether it
    returned or raised; every effect it never reached was withheld. Results
    line up with the batch by position (``apply_all`` returns one per action),
    so a composite applier's result is attributed to the action that was
    planned, whatever inner action it names.
    """

    planned: Sequence["Action"]
    _found: dict[int, tuple[CharterExecutionResult, str | None]] = field(
        default_factory=dict, init=False
    )

    def applied(self, batch: Sequence["Action"], results: Sequence[ActionResult]) -> None:
        for action, result in zip(batch, results):
            self._found[id(action)] = result_of(result)

    def raised(self, batch: Sequence["Action"], error: BaseException) -> None:
        for action in batch:
            self._found.setdefault(
                id(action),
                (
                    CharterExecutionResult.FAILED,
                    f"the completion apply raised before its result was known: {error}",
                ),
            )

    def link(self, authority: "Callable[[], TechLeadAuthorityStore | None]") -> None:
        """Link every executed decision's folded result; *authority* is read only
        when a planned effect carries one."""
        results: dict[str, list[tuple[CharterExecutionResult, str | None]]] = {}
        for action in self.planned:
            outcome = self._found.get(
                id(action),
                (
                    CharterExecutionResult.WITHHELD,
                    "withheld: a mandated tech-lead action in the same completion"
                    " did not commit",
                ),
            )
            for decision_id in action.charter_decisions:
                results.setdefault(decision_id, []).append(outcome)
        if not results:
            return
        store = authority()
        if store is None:
            raise ValueError(
                "linking executed charter decisions requires the"
                " TechLeadAuthorityStore wired into this applier"
            )
        link_execution_results(store.charter_ledger, results)


def link_audited_effect(
    authority: "TechLeadAuthorityStore",
    decisions: Iterable[TechLeadCharterDecision],
    outcome: "ActionResult | BaseException",
) -> None:
    """Link a charter-audited effect's attempt to its executed decisions."""
    found = (
        result_of(outcome)
        if isinstance(outcome, ActionResult)
        else (CharterExecutionResult.FAILED, f"{type(outcome).__name__}: {outcome}")
    )
    link_execution_results(
        authority.charter_ledger,
        {decision_id: [found] for decision_id in executed_decisions(decisions)},
    )


def link_parked(
    ledger: "TechLeadCharterLedger", decision_ids: Sequence[str], row: "LivenessRow"
) -> None:
    """The liveness owner parked an executed decision's effect (#7350)."""
    reason = (
        f"the orchestrator stopped retrying it ({row.last_outcome.value}):"
        f" {row.last_reason}"
    )
    link_execution_results(
        ledger,
        {decision_id: [(CharterExecutionResult.PARKED, reason)] for decision_id in decision_ids},
    )
