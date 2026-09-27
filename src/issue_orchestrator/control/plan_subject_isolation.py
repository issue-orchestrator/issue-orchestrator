"""Per-subject isolation while one tick's plan is applied (#7349).

A plan mixes actions for many subjects (issues and pull requests). When the
mutation gate refuses a write for one subject (``ReconciliationRequired``),
that subject must not be partially mutated any further this tick -- but every
OTHER subject's actions are independent and must still run. Before #7349 the
refusal halted the whole plan, so one paused issue (porchpin #410) starved
every review launch behind it for 133 ticks.

This module owns that scoping decision: which subjects an action touches, and
which subjects have been withheld for the rest of the current application.
Withholding is never silent: the applier reports every withheld action as a
failed step naming the subject.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from .tech_lead_mutation import TechLeadMutation

if TYPE_CHECKING:
    from .actions import Action

# The attributes through which an action names the GitHub numbers it touches.
# Issues and pull requests share one number space per repository, so a number
# identifies its subject unambiguously whichever attribute carries it.
_SUBJECT_ATTRIBUTES = ("issue_number", "number", "pr_number")


def action_subjects(action: "Action") -> frozenset[int]:
    """Every issue/PR number *action* would read or write."""
    subjects: set[int] = set()
    for attribute in _SUBJECT_ATTRIBUTES:
        value = getattr(action, attribute, None)
        # bool is an int subclass; a flag is never a subject number.
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            subjects.add(value)
    if isinstance(action, TechLeadMutation):
        # A tech-lead command names the issue its gate reconciles against.
        subject = action.reconciliation_subject()
        if subject > 0:
            subjects.add(subject)
    return frozenset(subjects)


@dataclass
class PlanSubjectIsolation:
    """Subjects withheld from the rest of ONE plan application."""

    _withheld: set[int] = field(default_factory=set)

    def withhold(self, subjects: Iterable[int]) -> None:
        """Withhold every remaining action that touches any of *subjects*."""
        self._withheld.update(subjects)

    def withheld_subject(self, action: "Action") -> int | None:
        """The withheld subject *action* touches, or None when it may run."""
        touched = action_subjects(action) & self._withheld
        return min(touched) if touched else None


__all__ = ["PlanSubjectIsolation", "action_subjects"]
