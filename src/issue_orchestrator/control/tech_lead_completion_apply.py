"""Apply a completion's actions so each class commits exactly when it should.

The authority-with-effects owner (ADR-0031 §2, #6779 R13, #6777). A completion
carries three classes of action (:func:`.tech_lead_completion_gate.partition_completion_effects`):

* the decision-MANDATED act-level actions are the gate, applied first;
* the tech lead's independent OBSERVATIONS (patterns, case files, follow-up
  issues, comments, surfaced proposals) are applied next, WHATEVER the gate
  did: none of them claims the completion succeeded, so an unrelated mandated
  write that failed is no reason to discard what the tech lead noticed;
* the SUCCESS-ONLY bookkeeping (anchor labels, completion comments, close)
  commits only when the gate committed and nothing raised, so a failed
  mandated action can never leave a success effect behind.

A raise past the runtime-kill boundary (``ReconciliationRequired`` /
``ClaimLostError``, #6777) is scoped the way the tick's plan applier scopes it
(:class:`.plan_subject_isolation.PlanSubjectIsolation`, #7349): the refused
subject is withheld for the rest of the completion and every other subject's
observations still run. Any other raise has no known subject, so everything
after it is withheld. The first raise is returned for the caller to finalize
the ONE terminal outcome from, then re-raise.

porchpin #410 (2026-09-28): a health review's success-only ``in-progress``
removal on its paused anchor raised, the single batch aborted, and the ledger
recorded all 14 of the review's findings as "withheld behind a mandated action"
that did not exist. Every withheld effect now records WHY it was withheld.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

from ..infra.logging_config import issue_log
from .claim_gate import ClaimLostError
from .plan_subject_isolation import PlanSubjectIsolation, action_subjects
from .reconciliation import ReconciliationRequired
from .tech_lead_completion_gate import (
    evaluate_required_act_level_outcome,
    partition_completion_effects,
)

if TYPE_CHECKING:
    from .action_applier import ActionApplier
    from .action_base import Action
    from .action_results import ActionResult
    from .tech_lead_charter_lifecycle import CompletionEffectLinks

logger = logging.getLogger(__name__)


def apply_completion_actions_gated(
    action_applier: "ActionApplier",
    actions: Sequence["Action"],
    *,
    issue_number: int,
) -> tuple[list["ActionResult"], BaseException | None]:
    """Apply a completion's actions: audit, then the gate, observations, success.

    The charter decisions are an audit of what was DECIDED, so they land first,
    whatever the gate does; a failed audit write withholds every effect (#7330
    review F2). What really happened to each executed decision's effects is
    linked back to its record once they have run (#7362).
    """
    from .tech_lead_charter_lifecycle import CompletionEffectLinks, link_or_log
    from .tech_lead_charter_records import partition_charter_records

    audit, effects = partition_charter_records(actions)
    audited, error = _apply_audit(action_applier, audit, issue_number)
    if error is not None or not all(result.success for result in audited):
        return audited, error or RuntimeError("tech-lead charter decisions were not recorded")
    links = CompletionEffectLinks(planned=effects)
    applied, error = _CompletionApply(action_applier, issue_number, links).run(effects)
    link_or_log(
        lambda: links.link(lambda: action_applier.tech_lead_ops),
        f"issue #{issue_number}'s completion effects",
    )
    return audited + applied, error


def _apply_audit(
    action_applier: "ActionApplier", audit: Sequence["Action"], issue_number: int
) -> tuple[list["ActionResult"], BaseException | None]:
    if not audit:
        return [], None
    _log_batch(issue_number, audit)
    try:
        return list(action_applier.apply_all(list(audit)) or []), None
    except Exception as exc:
        _log_raise(issue_number, exc)
        return [], exc


def _refused_subjects(action: "Action", error: BaseException) -> frozenset[int] | None:
    """The subjects a raise refused, or None when it names none (#7349).

    A reconciliation refusal and a lost claim are about ONE issue; the tick's
    plan applier withholds only that subject, and so does a completion. Any
    other raise (an adapter fault) has no known blast radius.
    """
    if isinstance(error, ReconciliationRequired):
        return action_subjects(action) | {error.entity_id}
    if isinstance(error, ClaimLostError):
        return action_subjects(action) | {error.issue_number}
    return None


@dataclass
class _CompletionApply:
    """One completion's apply: what landed, the first raise, and what is withheld."""

    applier: "ActionApplier"
    issue_number: int
    links: "CompletionEffectLinks"
    isolation: PlanSubjectIsolation = field(default_factory=PlanSubjectIsolation)
    error: BaseException | None = None
    #: Set when a raise with no known subject stopped the whole completion.
    halted: str | None = None

    def run(
        self, actions: Sequence["Action"]
    ) -> tuple[list["ActionResult"], BaseException | None]:
        effects = partition_completion_effects(actions)
        mandated = self._batch(effects.mandated, isolate=False)
        # A mandated batch that RAISED reports no results, as before #7362:
        # its raise already makes the verdict a hard failure, and consumers
        # must not post a second write behind it (#6777).
        applied = [] if self.error is not None else mandated
        gate_failed = (
            self.error is not None or evaluate_required_act_level_outcome(mandated).failed
        )
        applied += self._batch(effects.observations, isolate=True)
        if not effects.success_only:
            return applied, self.error
        if gate_failed or self.error is not None:
            reason = self._success_withheld_reason(mandated, gate_failed)
            logger.warning(
                issue_log(self.issue_number, "%s; withholding %d success-only completion effect(s)"),
                reason,
                len(effects.success_only),
            )
            self.links.withheld(effects.success_only, reason)
            return applied, self.error
        return applied + self._batch(effects.success_only, isolate=False), self.error

    def _success_withheld_reason(
        self, mandated: Sequence["ActionResult"], gate_failed: bool
    ) -> str:
        if gate_failed and self.error is None:
            summary = evaluate_required_act_level_outcome(mandated).failure_summary()
            return (
                "withheld: a mandated tech-lead action in the same completion"
                f" did not commit ({summary})"
            )
        assert self.error is not None
        return (
            "withheld: an earlier action in the same completion raised"
            f" {type(self.error).__name__}: {self.error}"
        )

    def _batch(self, batch: Sequence["Action"], *, isolate: bool) -> list["ActionResult"]:
        """Apply *batch* in order. On a scoped raise, ``isolate`` withholds only
        the refused subject and carries on; otherwise the rest is withheld."""
        results: list["ActionResult"] = []
        remaining = list(batch)
        while remaining:
            if self.halted is not None:
                self.links.withheld(remaining, self.halted)
                break
            runnable = self._not_withheld(remaining)
            if not runnable:
                break
            landed, error = self._apply(runnable)
            results += landed
            if error is None:
                break
            self.error = self.error or error
            raised = runnable[len(landed)] if len(landed) < len(runnable) else None
            refused = None if raised is None else _refused_subjects(raised, error)
            rest = runnable[len(landed) + 1:]
            reason = (
                f"withheld: the completion stopped when"
                f" {type(raised).__name__ if raised is not None else 'the apply'}"
                f" raised {type(error).__name__}: {error}"
            )
            if refused is None:
                self.halted = reason
            else:
                self.isolation.withhold(refused)
            if isolate and refused is not None:
                remaining = rest
                continue
            self.links.withheld(rest, reason)
            break
        return results

    def _not_withheld(self, actions: Sequence["Action"]) -> list["Action"]:
        runnable: list["Action"] = []
        for action in actions:
            subject = self.isolation.withheld_subject(action)
            if subject is None:
                runnable.append(action)
            else:
                self.links.withheld(
                    (action,),
                    f"withheld: #{subject} was refused earlier in the same completion",
                )
        return runnable

    def _apply(
        self, actions: Sequence["Action"]
    ) -> tuple[list["ActionResult"], BaseException | None]:
        """Apply one run of actions, capturing a raise past the runtime-kill boundary.

        What landed before the raise stands (#7362); the raising action's
        result is unknown, so it did not take effect.
        """
        _log_batch(self.issue_number, actions)
        landed: list["ActionResult"] = []
        try:
            # `or []` tolerates test doubles whose apply_all returns None.
            results = list(self.applier.apply_all(list(actions), on_result=landed.append) or [])
        except Exception as exc:
            self.links.applied(actions, landed)
            if len(landed) < len(actions):
                self.links.raised(actions[len(landed)], exc)
            _log_raise(self.issue_number, exc)
            return landed, exc
        self.links.applied(actions, results)
        return results, None


def _log_batch(issue_number: int, actions: Sequence["Action"]) -> None:
    logger.info(
        issue_log(issue_number, "Applying %d completion action(s): %s"),
        len(actions),
        [type(action).__name__ for action in actions],
    )


def _log_raise(issue_number: int, exc: BaseException) -> None:
    logger.warning(
        issue_log(
            issue_number,
            "Completion-action apply raised; finalizing terminal FAILED before re-raising: %s",
        ),
        exc,
    )


__all__ = ["apply_completion_actions_gated"]
