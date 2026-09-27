"""The planner's half of action liveness (#7350): gate a plan, settle what it did.

The planner re-derives its actions every tick from facts, and until this module
a failed action left no trace it could read: ``ActionResult.fail`` was logged,
emitted and forgotten, so the next tick planned the identical action again.
Census loops #2 (promotion settle raising every tick) and #4 (every mutation on
a subject paused behind ``io:needs-reconcile`` refused, and the refusal halting
the rest of the plan) were both that.

Two calls close it, both through the one :class:`ActionLivenessOwner`:

* :meth:`PlannedActionLiveness.admit` runs between planning and applying. Each
  action gets a :class:`LivenessKey`; a parked or backing-off action leaves the
  plan and is reported in ``Plan.skipped`` with the owner's reason;
* the admitted plan carries a :class:`PlanLiveness`, and the apply loop settles
  every attempted action through it, success or failure. An ungated plan cannot
  be applied, so no applied action escapes the owner.

The key:

* subject - the issue the action reconciles against, else the issue/PR it
  names, else ``engine`` for the handful of engine-wide actions;
* action - the :class:`ActionType` value;
* fingerprint - the action's own :meth:`~.action_base.Action.liveness_facts`
  (by default every field but the free-text ``reason``), plus the subject's
  labels as this tick observed them, minus the owner's own escalation label.
  The labels are what let a person release a park by acting on the issue:
  removing ``io:needs-reconcile`` changes the facts, so the next plan is a new
  question. An action whose ``liveness_facts`` is ``None`` (launches, requests
  for a human) is governed by its own owner and passes through ungated.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.action_liveness import (
    ActionIdentity,
    ActionOutcome,
    LivenessKey,
    OutcomeKind,
    fact_fingerprint,
)
from ..ports.repository_host import host_rate_limit_of
from .action_base import Action
from .actions import (
    AddLabelAction,
    PromoteTechLeadFindingAction,
    RemoveLabelAction,
    ReportPromotedFindingEvidenceAction,
    SettleTechLeadPromotionAction,
    SyncLabelsAction,
)
from .provider_impact import ApplyProviderImpactAction
from .action_results import ActionResult, ActionResultType
from .action_liveness import ActionLivenessOwner, transient_outcome
from .reconciliation import (
    ReconciliationRequired,
    ReconciliationResponse,
    get_pause_label,
    response_to,
)

if TYPE_CHECKING:
    from ..events import EventContext
    from ..ports.issue import Issue
    from .planner_types import OrchestratorSnapshot, Plan

logger = logging.getLogger(__name__)

#: Subject of an action that names no issue or PR (anchor creation, ledger-only
#: discards). One identity per action type: still bounded, still visible.
ENGINE_SUBJECT = "engine"

def _subject_number(action: Action) -> tuple[str, int] | None:
    effect = getattr(action, "effect", None)
    if isinstance(effect, Action):
        # A wrapper (a charter-audited effect) is about its effect's subject.
        return _subject_number(effect)
    subject = getattr(action, "reconciliation_subject", None)
    if callable(subject):
        number = subject()
        if isinstance(number, int) and number > 0:
            return "issue", number
    issue_number = getattr(action, "issue_number", None)
    if isinstance(issue_number, int) and issue_number > 0:
        return "issue", issue_number
    number = getattr(action, "number", None)
    if isinstance(number, int) and number > 0:
        return ("pr" if getattr(action, "is_pr", False) else "issue"), number
    pr_number = getattr(action, "pr_number", None)
    if isinstance(pr_number, int) and pr_number > 0:
        return "pr", pr_number
    return None


def _labels(action: Action) -> str:
    return str(getattr(action, "label"))


def _label_sets(action: Action) -> str:
    adds = ",".join(sorted(getattr(action, "add_labels")))
    removes = ",".join(sorted(getattr(action, "remove_labels")))
    return f"+{adds}-{removes}"


def _signature(action: Action) -> str:
    return str(getattr(action, "signature"))


#: Operations that stay the SAME operation while the facts behind them change,
#: with what names them. Only for these can a success under new facts
#: supersede a park under old ones (retired after ``stale_after``). Every other
#: action's operation IS its facts: two different comments on one issue are two
#: operations, so one's success can never launder the other's failures; an old
#: park of such an action is released by an operator or abandoned.
_STABLE_OPERATIONS: dict[type[Action], Callable[[Action], str]] = {
    AddLabelAction: _labels,
    RemoveLabelAction: _labels,
    ApplyProviderImpactAction: _labels,
    SyncLabelsAction: _label_sets,
    PromoteTechLeadFindingAction: _signature,
    ReportPromotedFindingEvidenceAction: _signature,
    SettleTechLeadPromotionAction: _signature,
}


def liveness_operation(action: Action) -> str:
    """WHICH operation this action is on its subject (the identity's ``action``).

    A wrapper (a charter-audited effect) is the operation of its effect, so a
    stable effect stays stable through the wrapper.
    """
    effect = getattr(action, "effect", None)
    if isinstance(effect, Action):
        return f"{action.action_type.value}>{liveness_operation(effect)}"
    stable = _STABLE_OPERATIONS.get(type(action))
    if stable is not None:
        return f"{action.action_type.value}:{stable(action)}"
    return f"{action.action_type.value}#{fact_fingerprint(action.liveness_facts())[:12]}"


def planned_action_key(
    action: Action,
    labels_by_number: Mapping[int, tuple[str, ...]],
    *,
    escalation_label: str,
) -> LivenessKey | None:
    """The liveness key of one planned action, from its facts and the tick's labels.

    ``None`` when the action's own :meth:`~.action_base.Action.liveness_facts`
    says another owner governs it. The owner's own ``escalation_label`` is not
    a fact: the park that put it on the issue must not look like progress.
    """
    facts = action.liveness_facts()
    if facts is None:
        return None
    subject = _subject_number(action)
    observed = None if subject is None else labels_by_number.get(subject[1])
    labels = None if observed is None else frozenset(observed) - {escalation_label}
    return LivenessKey(
        identity=ActionIdentity(
            subject=ENGINE_SUBJECT if subject is None else f"{subject[0]}:{subject[1]}",
            action=liveness_operation(action),
        ),
        fingerprint=fact_fingerprint(
            {
                "action": facts,
                "subject_labels": labels,
            }
        ),
        escalation_issue=None if subject is None else subject[1],
    )


def _resolved_issue(action: Action) -> int | None:
    if not action.liveness_resolves_subject():
        return None
    subject = _subject_number(action)
    if subject is None:
        raise ValueError(f"{type(action).__name__} resolves a subject it does not name")
    return subject[1]


def observed_labels(snapshot: "OrchestratorSnapshot") -> dict[int, tuple[str, ...]]:
    """Every issue's labels this tick observed, across the snapshot's issue views."""
    views: Iterable[Iterable["Issue"]] = (
        snapshot.issues,
        snapshot.stale_in_progress_issues,
        snapshot.stale_claim_issues,
        snapshot.tech_lead_subjects,
    )
    labels: dict[int, tuple[str, ...]] = {}
    for view in views:
        for issue in view:
            labels[issue.number] = tuple(issue.labels)
    return labels


def outcome_of_result(result: ActionResult) -> ActionOutcome:
    """A returned result: a failure is transient unless an applier said otherwise.

    A GitHub rate limit (#7303's typed ``HostRateLimit``) is a transient failure
    that says when the host will answer again: it waits until then and spends
    nothing, within the policy's declared-wait bound. A skipped action found
    nothing to do, which is progress for liveness.
    """
    if result.result_type is not ActionResultType.FAILURE:
        return ActionOutcome.done()
    reason = result.error or "action failed without an error"
    return transient_outcome(reason, result.host_rate_limit)


def outcome_of_error(error: Exception) -> ActionOutcome:
    """An action that raised instead of returning.

    A gate refusal means what :func:`~.reconciliation.response_to` says it
    means. Refused BECAUSE the subject is paused behind the reconciliation
    label is not transient: every mutation planned for that subject is refused
    the same way until a person reconciles it and removes the label (census
    loop #4). A subject the gate could not read this tick is deferred (#7379):
    transient, waiting on the read's rate limit when one is behind it. Observed
    drift, a lost claim, or an unexpected error may heal.
    """
    if (
        isinstance(error, ReconciliationRequired)
        and response_to(error) is ReconciliationResponse.ALREADY_PAUSED
    ):
        return ActionOutcome.needs_human(
            f"subject is paused behind {get_pause_label()}; every planned mutation"
            " is refused until a person reconciles it and removes the label"
        )
    return transient_outcome(f"{type(error).__name__}: {error}", host_rate_limit_of(error))


@dataclass(frozen=True, slots=True)
class PlanLiveness:
    """The keys an admitted plan's actions were admitted under, and their owner."""

    owner: ActionLivenessOwner
    #: ``None`` for a self-governed action: its own owner settles it.
    keys: tuple[LivenessKey | None, ...]
    #: The issue each action settles every park on when it succeeds
    #: (:meth:`~.action_base.Action.liveness_resolves_subject`), else ``None``.
    resolves: tuple[int | None, ...]

    def __post_init__(self) -> None:
        if len(self.resolves) != len(self.keys):
            raise ValueError("a gated plan needs one resolution entry per action")

    def settle(self, index: int, outcome: ActionOutcome) -> None:
        key = self.keys[index]
        if key is not None:
            self.owner.record(key, outcome)
        resolved = self.resolves[index]
        if resolved is not None and outcome.kind is OutcomeKind.DONE:
            self.owner.release_issue(resolved)


@dataclass(frozen=True, slots=True)
class PlannedActionLiveness:
    """Gates each plan through the owner before it is applied."""

    owner: ActionLivenessOwner
    #: The label the owner's escalation puts on an issue; never a fact.
    escalation_label: str

    def admit(
        self, plan: "Plan", snapshot: "OrchestratorSnapshot", context: "EventContext"
    ) -> "Plan":
        """Admit ``plan`` through the owner, then settle this cycle's owed
        writes; ``context`` is the run and tick they are announced in."""
        from .planner_types import Plan, SkippedItem

        labels = observed_labels(snapshot)
        admitted: list[Action] = []
        keys: list[LivenessKey | None] = []
        resolves: list[int | None] = []
        admitted_keys: set[LivenessKey] = set()
        held: list[SkippedItem] = []
        for action in plan.actions:
            key = planned_action_key(
                action, labels, escalation_label=self.escalation_label
            )
            if key is None:
                admitted.append(action)
                keys.append(None)
                resolves.append(_resolved_issue(action))
                continue
            decision = self.owner.admit(key)
            if decision.admitted and key not in admitted_keys:
                admitted.append(action)
                keys.append(key)
                resolves.append(_resolved_issue(action))
                admitted_keys.add(key)
                continue
            if decision.admitted:
                # The same question twice in one plan: it is asked once, so
                # one plan cannot spend more than one attempt of a budget.
                held.append(
                    SkippedItem(
                        item_type=f"action:{key.identity.action}",
                        number=key.escalation_issue or 0,
                        reason="duplicate of an action already in this plan",
                    )
                )
                continue
            logger.debug(
                "[LIVENESS] Holding %s on %s: %s",
                key.identity.action,
                key.identity.subject,
                decision.describe(),
            )
            held.append(
                SkippedItem(
                    item_type=f"action:{key.identity.action}",
                    number=key.escalation_issue or 0,
                    reason=decision.describe(),
                )
            )
        # Once per planning cycle, AFTER this plan's keys were asked (and so
        # marked live): retire what nobody asks about, retry owed effects. An
        # owed pause this tick observed on its issue is already there.
        self.owner.settle_observed_pauses(labels)
        self.owner.reconcile_effects(context)
        return Plan(
            actions=tuple(admitted),
            skipped=plan.skipped + tuple(held),
            liveness=PlanLiveness(self.owner, tuple(keys), tuple(resolves)),
        )


__all__ = [
    "ENGINE_SUBJECT",
    "PlanLiveness",
    "PlannedActionLiveness",
    "observed_labels",
    "outcome_of_error",
    "outcome_of_result",
    "planned_action_key",
]
