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
capacity each stage's launches consume, and it refuses a second launch of a
subject the plan already launches: refused, reported in ``Plan.skipped`` with
the reason, and never applied.

The subject is the GitHub number the launch acts on - the issue for an issue,
rework, retrospective-review, tech-lead or validation-retry launch, the PR for
a code review. Issues and PRs share one number space in a repository, so two
launches naming one number are two launches of one thing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

from .action_base import Action
from .actions import ActionType, LaunchSessionAction, LaunchValidationRetryAction
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
        # one-launch-per-subject rule; fail loudly instead.
        raise TypeError(f"launch action {type(action).__name__} names no subject")
    return None


def _describe(action: Action) -> str:
    if isinstance(action, LaunchSessionAction):
        return f"{action.session_type.value} launch"
    return f"{action.action_type.value} launch"


@dataclass
class PlanLaunches:
    """Every launch stage of one plan passes its actions through :meth:`admit`."""

    #: The plan's skipped list; a refused duplicate is reported into it.
    skipped: list[SkippedItem]
    _by_subject: dict[int, Action] = field(default_factory=dict)

    def admit(self, stage: Sequence[Action], *, into: list[Action]) -> int:
        """Append ``stage``'s admitted actions to ``into``; return the slots they use.

        Non-launch actions (a provider-skip label, a tech-lead withdrawal) pass
        through untouched. A launch of a subject this plan already launches is
        refused, so it neither runs nor consumes capacity.
        """
        launches = 0
        for action in stage:
            subject = launch_subject(action)
            if subject is None:
                into.append(action)
                continue
            first = self._by_subject.get(subject)
            if first is not None:
                reason = (
                    f"duplicate launch: #{subject} already has a {_describe(first)}"
                    " in this plan"
                )
                logger.warning("[PLAN] Refusing %s of #%d: %s", _describe(action), subject, reason)
                self.skipped.append(
                    SkippedItem(item_type=_describe(action), number=subject, reason=reason)
                )
                continue
            self._by_subject[subject] = action
            into.append(action)
            launches += 1
        return launches


__all__ = [
    "CAPACITY_CONSUMING_LAUNCH_TYPES",
    "PlanLaunches",
    "launch_subject",
]
