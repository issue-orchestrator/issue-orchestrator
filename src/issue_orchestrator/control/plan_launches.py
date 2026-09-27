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
* a new ISSUE session for an issue the plan already launches other work for.
  The issue pipeline is planned last and already skips issues that have a
  review, rework or retrospective review pending; this closes the kinds it
  does not see (a queued tech-lead run, a validation retry) in the one plan
  where both would otherwise start.

Other combinations - a rework and a tech-lead investigation of one issue -
are separate sessions by design and are left to the stages that plan them.

The subject is the GitHub number the launch acts on - the issue for an issue,
rework, retrospective-review, tech-lead or validation-retry launch, the PR for
a code review. Issues and PRs share one number space in a repository.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

from .action_base import Action
from .actions import ActionType, LaunchSessionAction, LaunchValidationRetryAction, SessionType
from .planner_types import SkippedItem

logger = logging.getLogger(__name__)

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


def _kind(action: Action) -> str:
    if isinstance(action, LaunchSessionAction):
        return action.session_type.value
    return action.action_type.value


def _describe(action: Action) -> str:
    return f"{_kind(action)} launch"


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

    @staticmethod
    def _conflict(action: Action, earlier: list[Action]) -> Action | None:
        """The admitted launch ``action`` would duplicate, if any."""
        new_issue_work = (
            isinstance(action, LaunchSessionAction)
            and action.session_type is SessionType.ISSUE
        )
        for admitted in earlier:
            if new_issue_work or _kind(admitted) == _kind(action):
                return admitted
        return None


__all__ = [
    "CAPACITY_CONSUMING_LAUNCH_TYPES",
    "PlanLaunches",
    "launch_subject",
]
