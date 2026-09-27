"""The launches one plan makes: what each costs, and one per subject (#7454).

A plan's launch stages (reviews, retrospective reviews, reworks, validation
retries, tech-lead runs, new issues) each read their own queue, so nothing
stopped two of them - or one queue holding the same request twice - from
planning two launches of the same issue or PR in one plan. The first launch
started the session; the second found its work already consumed and was
reported as ``launch_session failed`` right after ``Launched`` (exam Case U: a
restart put one review on its queue twice; porchpin: a health-review anchor
was planned both as a tech-lead run and as an ordinary issue). Each duplicate
also spent a worker slot the plan could have given to real work.

This owner is the one place a plan's launches pass through. It counts the
capacity each stage's launches consume, and it refuses two kinds of second
launch - refused, reported in ``Plan.skipped`` with the reason, never applied:

* the SAME launch again: the same kind of session for the same issue or PR;
* a second CODER session for one issue: a new issue session, a rework and a
  validation retry all drive the issue's branch, so at most one of them starts
  per plan;
* a new ISSUE session for an issue the plan already launches any other work
  for. The issue pipeline already skips issues with a review, rework or
  retrospective review pending; this closes the kinds it does not see (a
  queued tech-lead run of its anchor, a validation retry).

Other combinations - a rework and a tech-lead investigation of one issue -
are separate sessions by design and are left to the stages that plan them.

Refusing after a stage has sliced its queue to the free capacity would still
waste the slot, so stages also consult this owner BEFORE they pick:
:func:`first_per_subject` for a queue, and :meth:`PlanLaunches.subjects` /
:meth:`PlanLaunches.coder_subjects` with :func:`withhold_launching` for the
validation-retry and issue stages.

The subject is the GitHub number the launch acts on - the issue for an issue,
rework, retrospective-review, tech-lead or validation-retry launch, the PR for
a code review. Issues and PRs share one number space in a repository.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence, TypeVar

from .action_base import Action
from .actions import ActionType, LaunchSessionAction, LaunchValidationRetryAction, SessionType
from .planner_types import SkippedItem

logger = logging.getLogger(__name__)

_Request = TypeVar("_Request")

#: Launch action kinds that occupy a worker slot when planned - the single
#: source of truth the capacity counter uses so a new launch kind can't
#: silently escape the worker budget (#6892 review F1/F2). A provider-skip
#: label action is deliberately absent (it launches nothing).
CAPACITY_CONSUMING_LAUNCH_TYPES: frozenset[ActionType] = frozenset(
    {ActionType.LAUNCH_SESSION, ActionType.LAUNCH_VALIDATION_RETRY}
)


def launch_subject(action: Action) -> int | None:
    """The issue or PR number ``action`` launches a session for, else ``None``."""
    if isinstance(action, LaunchSessionAction):
        return action.number
    if isinstance(action, LaunchValidationRetryAction):
        return action.issue_number
    if action.action_type in CAPACITY_CONSUMING_LAUNCH_TYPES:
        # A launch kind added without a subject here would bypass the
        # duplicate-launch rule; fail loudly instead.
        raise TypeError(f"launch action {type(action).__name__} names no subject")
    return None


#: Session kinds that drive an issue's branch; one per issue per plan.
_CODER_KINDS = frozenset(
    {
        SessionType.ISSUE.value,
        SessionType.REWORK.value,
        ActionType.LAUNCH_VALIDATION_RETRY.value,
    }
)


def _kind(action: Action) -> str:
    if isinstance(action, LaunchSessionAction):
        return action.session_type.value
    return action.action_type.value


def _describe(action: Action) -> str:
    return f"{_kind(action)} launch"


def first_per_subject(
    requests: Iterable[_Request], subject: Callable[[_Request], int | None]
) -> list[_Request]:
    """``requests`` in order, keeping only the first request per subject.

    A stage's workflow slices its queue to the free capacity; a duplicate that
    reached the slice would take a slot :class:`PlanLaunches` then refuses,
    starving the next distinct request. A request with no subject is kept.
    """
    seen: set[int] = set()
    kept: list[_Request] = []
    for request in requests:
        key = subject(request)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        kept.append(request)
    return kept


def withhold_launching(
    candidates: Iterable[_Request],
    launching: frozenset[int],
    skipped: list[SkippedItem],
    *,
    item_type: str,
    subject: Callable[[_Request], int],
) -> tuple[list[_Request], dict[int, str]]:
    """Split off the candidates an earlier stage of this plan already launches.

    Returns the candidates a stage may still pick from, and the skip reason
    per withheld subject (the issue pipeline's queue decision log reads it).
    Each withheld candidate is reported in ``skipped``.
    """
    kept: list[_Request] = []
    reasons: dict[int, str] = {}
    for candidate in candidates:
        number = subject(candidate)
        if number in launching:
            skipped.append(
                SkippedItem(
                    item_type=item_type,
                    number=number,
                    reason="other work for this issue launches this tick",
                )
            )
            reasons[number] = "launching_this_tick"
            continue
        kept.append(candidate)
    return kept, reasons


@dataclass
class PlanLaunches:
    """Every launch stage of one plan passes its actions through :meth:`admit`."""

    #: The plan's skipped list; a refused launch is reported into it.
    skipped: list[SkippedItem]
    _by_subject: dict[int, list[Action]] = field(default_factory=dict)

    def admit(self, stage: Sequence[Action], *, into: list[Action]) -> int:
        """Append ``stage``'s admitted actions to ``into``; return the slots they use.

        Non-launch actions (a provider-skip label, a tech-lead withdrawal) pass
        through untouched. A refused launch neither runs nor consumes capacity.
        """
        launches = 0
        for action in stage:
            subject = launch_subject(action)
            if subject is None:
                into.append(action)
                continue
            earlier = self._by_subject.setdefault(subject, [])
            conflict = self._conflict(action, earlier)
            if conflict is not None:
                reason = (
                    f"duplicate launch: #{subject} already has a {_describe(conflict)}"
                    " in this plan"
                )
                logger.warning("[PLAN] Refusing %s of #%d: %s", _describe(action), subject, reason)
                self.skipped.append(
                    SkippedItem(item_type=_describe(action), number=subject, reason=reason)
                )
                continue
            earlier.append(action)
            into.append(action)
            launches += 1
        return launches

    def subjects(self) -> frozenset[int]:
        """Every issue/PR this plan already launches a session for."""
        return frozenset(n for n, admitted in self._by_subject.items() if admitted)

    def coder_subjects(self) -> frozenset[int]:
        """Every issue this plan already launches a coder session for."""
        return frozenset(
            n
            for n, admitted in self._by_subject.items()
            if any(_kind(a) in _CODER_KINDS for a in admitted)
        )

    @staticmethod
    def _conflict(action: Action, earlier: list[Action]) -> Action | None:
        """The admitted launch ``action`` would duplicate, if any."""
        kind = _kind(action)
        for admitted in earlier:
            earlier_kind = _kind(admitted)
            if (
                kind == SessionType.ISSUE.value
                or earlier_kind == kind
                or (kind in _CODER_KINDS and earlier_kind in _CODER_KINDS)
            ):
                return admitted
        return None


__all__ = [
    "CAPACITY_CONSUMING_LAUNCH_TYPES",
    "PlanLaunches",
    "first_per_subject",
    "launch_subject",
    "withhold_launching",
]
