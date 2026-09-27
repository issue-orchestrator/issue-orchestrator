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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.action_liveness import (
    ActionIdentity,
    ActionOutcome,
    LivenessKey,
    OutcomeKind,
    fact_fingerprint,
)
from ..domain.host_rate_limit import HostRateLimit
from ..ports.repository_host import host_rate_limit_of
from .action_base import Action
from .action_results import ActionResult, ActionResultType
from .action_liveness import ActionLivenessOwner
from .reconciliation import ReconciliationRequired, get_pause_label

if TYPE_CHECKING:
    from ..ports.issue import Issue
    from .planner_types import OrchestratorSnapshot, Plan

logger = logging.getLogger(__name__)

#: Subject of an action that names no issue or PR (anchor creation, ledger-only
#: discards). One identity per action type: still bounded, still visible.
ENGINE_SUBJECT = "engine"

def _subject_number(action: Action) -> tuple[str, int] | None:
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
            action=action.action_type.value,
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
    return _transient(reason, result.host_rate_limit)


def _transient(reason: str, limit: HostRateLimit | None) -> ActionOutcome:
    if limit is None:
        return ActionOutcome.transient(reason)
    return ActionOutcome.transient(
        f"{reason} (GitHub rate limit until {limit.resets_at.isoformat()})",
        retry_at=limit.resets_at,
    )


def outcome_of_error(error: Exception) -> ActionOutcome:
    """An action that raised instead of returning.

    A refusal because the subject is paused behind the reconciliation label is
    not transient: every mutation planned for that subject is refused the same
    way until a person reconciles it and removes the label (census loop #4).
    Any other drift, a lost claim, or an unexpected error may heal.
    """
    pause_label = get_pause_label()
    if isinstance(error, ReconciliationRequired) and pause_label in error.actual.labels:
        return ActionOutcome.needs_human(
            f"subject is paused behind {pause_label}; every planned mutation is"
            " refused until a person reconciles it and removes the label"
        )
    return _transient(f"{type(error).__name__}: {error}", host_rate_limit_of(error))


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

    def admit(self, plan: "Plan", snapshot: "OrchestratorSnapshot") -> "Plan":
        from .planner_types import Plan, SkippedItem

        # Escalation effects that did not commit last time are retried once
        # per planning cycle, before anything new is attempted.
        self.owner.reconcile_effects()
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
