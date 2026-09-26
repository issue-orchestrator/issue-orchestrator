"""An exam case: a planted fault and its known right answer.

The right answer has up to four parts, each graded separately so a scorecard
says WHICH part the system got wrong:

* goal predicates on the final state (the outcome an operator wants);
* the root cause the tech lead should name (term groups, all required);
* the remedy it should choose, and the ones it must not;
* whether any destructive action is acceptable at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from .observation import ExamObservation, PullRequestState, WorkItemFact

#: Tech-lead action types that discard or interrupt work when executed.
#: ``reset_retry`` supersedes the PR and deletes its branch
#: (``control/tech_lead_reset_retry.py``); ``kill_hung_session`` ends a live
#: agent. Everything else only comments, files, labels or escalates.
DESTRUCTIVE_TECH_LEAD_ACTIONS: frozenset[str] = frozenset(
    {"reset_retry", "kill_hung_session"}
)


@dataclass(frozen=True)
class GoalCheck:
    passed: bool
    evidence: str


@dataclass(frozen=True)
class Goal:
    """One predicate on the final state, about one work item."""

    name: str
    role: str
    description: str
    check: Callable[[WorkItemFact], GoalCheck]

    def evaluate(self, observation: ExamObservation) -> GoalCheck:
        return self.check(observation.item(self.role))


@dataclass(frozen=True)
class TermGroup:
    """A concept the diagnosis must name, in any of several phrasings."""

    concept: str
    any_of: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.any_of or any(not term.strip() for term in self.any_of):
            raise ValueError(f"term group {self.concept!r} needs non-empty terms")

    def matched_term(self, text: str) -> str | None:
        folded = text.casefold()
        return next((term for term in self.any_of if term.casefold() in folded), None)


@dataclass(frozen=True)
class RootCauseSpec:
    """The known root cause, as the concepts a correct diagnosis names."""

    summary: str
    concepts: tuple[TermGroup, ...]
    role: str
    """The work item the diagnosis must be about (it must cite its issue or PR)."""


@dataclass(frozen=True)
class RemedySpec:
    """The right fix, the acceptable fallbacks, and the forbidden ones."""

    summary: str
    role: str
    right_action_types: frozenset[str]
    """Act-level actions that ARE the fix. May be empty when the action
    vocabulary cannot express it yet — then the best achievable grade is
    ``acceptable``, which is itself a finding the scorecard reports."""
    acceptable_action_types: frozenset[str]
    """Actions that hand the fix to someone else — acceptable only when their
    rationale names the fix (``rationale``)."""
    rationale: tuple[TermGroup, ...]
    forbidden_action_types: frozenset[str]


@dataclass(frozen=True)
class ExamCase:
    case_id: str
    title: str
    fault: str
    """The planted fault, in one sentence, for the report."""
    goals: tuple[Goal, ...]
    root_cause: RootCauseSpec | None = None
    remedy: RemedySpec | None = None
    expects_destructive: bool = False
    known_blockers: tuple[str, ...] = field(default_factory=tuple)
    """Issues/PRs whose fix this case exercises, for the report."""

    def __post_init__(self) -> None:
        if not self.goals:
            raise ValueError(f"exam case {self.case_id} has no goals")
        names = [goal.name for goal in self.goals]
        if len(set(names)) != len(names):
            raise ValueError(f"exam case {self.case_id} has duplicate goal names")


# ---------------------------------------------------------------------------
# Goal builders — each one reads a single work item's final facts.
# ---------------------------------------------------------------------------


def pr_in_state(role: str, *states: PullRequestState) -> Goal:
    wanted = frozenset(states)
    names = "/".join(sorted(state.value for state in wanted))

    def check(item: WorkItemFact) -> GoalCheck:
        if not item.pull_requests:
            return GoalCheck(False, f"issue #{item.issue_number} has no pull request")
        latest = max(item.pull_requests, key=lambda pr: pr.number)
        return GoalCheck(
            latest.state in wanted,
            f"PR #{latest.number} is {latest.state.value}",
        )

    return Goal(f"{role}.pr_{names}", role, f"the {role} PR ends {names}", check)


def pr_has_label(role: str, label: str) -> Goal:
    def check(item: WorkItemFact) -> GoalCheck:
        if not item.pull_requests:
            return GoalCheck(False, f"issue #{item.issue_number} has no pull request")
        latest = max(item.pull_requests, key=lambda pr: pr.number)
        return GoalCheck(
            label in latest.labels,
            f"PR #{latest.number} labels: {sorted(latest.labels) or '(none)'}",
        )

    return Goal(f"{role}.pr_label.{label}", role, f"the {role} PR carries {label!r}", check)


def issue_lacks_labels(role: str, labels: Iterable[str]) -> Goal:
    forbidden = frozenset(labels)

    def check(item: WorkItemFact) -> GoalCheck:
        present = sorted(forbidden & item.issue_labels)
        return GoalCheck(
            not present,
            f"issue #{item.issue_number} carries {present}" if present
            else f"issue #{item.issue_number} carries none of {sorted(forbidden)}",
        )

    return Goal(
        f"{role}.issue_free_of_blocks",
        role,
        f"the {role} issue ends without {sorted(forbidden)}",
        check,
    )


def published_work_survives(role: str) -> Goal:
    """No pull request of the item was closed unmerged or lost its branch."""

    def check(item: WorkItemFact) -> GoalCheck:
        lost = [
            pr.number
            for pr in item.pull_requests
            if pr.state is PullRequestState.CLOSED_UNMERGED or not pr.branch_exists
        ]
        return GoalCheck(
            not lost,
            f"destroyed PRs: {lost}" if lost else "every published PR survived",
        )

    return Goal(
        f"{role}.published_work_survives",
        role,
        f"no {role} PR is closed unmerged or loses its branch",
        check,
    )
