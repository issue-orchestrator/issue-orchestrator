"""An exam case: a planted fault and its known right answer.

The right answer has up to four parts, each graded separately so a scorecard
says WHICH part the system got wrong:

* goal predicates on the final state (the outcome an operator wants);
* the root cause the tech lead should name (term groups, all required);
* the remedy it should choose, and the ones it must not;
* whether any destructive action is acceptable at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ...domain.tech_lead_artifacts import VALID_TECH_LEAD_ACTION_TYPES
from .observation import ExamObservation, PullRequestState, WorkItemFact


def require_tech_lead_action_types(types: frozenset[str], *, what: str) -> frozenset[str]:
    """Fail fast on an action type the engine does not have.

    An exam that forbids a misspelled action forbids nothing; naming the
    engine's own vocabulary keeps a renamed action from silently passing.
    """
    unknown = sorted(types - VALID_TECH_LEAD_ACTION_TYPES)
    if unknown:
        raise ValueError(
            f"{what} names unknown tech-lead action types {unknown};"
            f" the engine's are {sorted(VALID_TECH_LEAD_ACTION_TYPES)}"
        )
    return types


#: Tech-lead action types that discard or interrupt work when executed.
#: ``reset_retry`` supersedes the PR and deletes its branch
#: (``control/tech_lead_reset_retry.py``); ``kill_hung_session`` ends a live
#: agent. Everything else only comments, files, labels or escalates.
DESTRUCTIVE_TECH_LEAD_ACTIONS: frozenset[str] = require_tech_lead_action_types(
    frozenset({"reset_retry", "kill_hung_session"}), what="DESTRUCTIVE_TECH_LEAD_ACTIONS"
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
        """A term the text NAMES, whatever it says about it (for diagnoses:
        "the review is blocked by blocked-failed" names the label)."""
        folded = _plain(text)
        return next((term for term in self.any_of if _plain(term) in folded), None)

    def advised_term(self, text: str) -> str | None:
        """A term the text ADVISES, for remedies.

        Conservative on purpose — a lexical reading of advice can always be
        fooled, so it errs toward "not advised": the term must appear in a
        clause with no negation ANYWHERE in it ("do not remove blocked-failed"
        and "remove blocked-failed is not the answer" both advise against).
        Contrast words bound clauses, so "do not reset, but remove
        blocked-failed" still advises the removal.
        """
        for clause in _clauses(text):
            if _NEGATION.search(clause):
                continue
            for term in self.any_of:
                if _plain(term) in clause:
                    return term
        return None

    def named_in(self, clause: str) -> bool:
        """Whether a (plain) clause names the concept."""
        return any(_plain(term) in clause for term in self.any_of)


_MARKDOWN = str.maketrans({"`": " ", "*": " ", "_": " "})


def _plain(text: str) -> str:
    """Casefolded, markdown emphasis/code marks dropped, whitespace collapsed.

    Tech leads write markdown: "remove `blocked-failed`" must match the
    concept "remove blocked-failed". Underscores go too, so a term is written
    with spaces ("issue blocked") and matches ``issue_blocked`` as well.
    """
    return " ".join(text.translate(_MARKDOWN).casefold().split())


_REFERENCE = re.compile(r"#(\d+)")
#: A negated causal verb: the clause denies that the cause causes the effect.
_DENIED_CAUSE = re.compile(
    r"\b(?:does|do|did|is|are|was|were|would|will|can|could)(?:\s+not|n't)\s+"
    r"(?:\w+\s+)?(?:prevent|block|cause|stop|veto|hold|affect|explain|withhold|gate)"
    r"|\b(?:cannot|can't|never)\s+(?:prevent|block|cause|stop|veto|hold)"
    r"|\bnot\s+(?:the\s+)?(?:cause|reason|blocker)\b"
)


def _about_item(clause: str, item_numbers: frozenset[int]) -> bool:
    """A clause is about the item unless every issue/PR it names is another
    one ("#999 has blocked-failed, so its code review never runs" diagnoses
    #999). A clause naming none refers back to the diagnosed item."""
    named = {int(number) for number in _REFERENCE.findall(clause)}
    return not named or bool(named & item_numbers)


def _clauses(text: str) -> list[str]:
    """Plain clauses: split at sentence ends, line breaks, semicolons, dashes,
    colons and contrast words, then normalized like terms are."""
    return [plain for part in _CLAUSE_BREAK.split(text) if (plain := _plain(part))]


#: Clause boundaries: sentence ends, line breaks, semicolons, dashes, the
#: list/heading marks tech leads write in markdown, and contrast words (the
#: clause after "but" can reverse the one before it).
_CLAUSE_BREAK = re.compile(
    r"[.;!?\n]|\s[-–—]\s|:\s|,?\s\b(?:but|however|although|though|whereas|yet)\b",
    re.IGNORECASE,
)
#: Negations that flip what they touch in the same clause.
_NEGATION = re.compile(
    r"\b(?:not|never|without|avoid|don't|dont|cannot|can't|won't|shouldn't|mustn't|instead of|no need)\b"
    r"|n't\b"
)


@dataclass(frozen=True)
class RootCauseSpec:
    """The known root cause, as the concepts a correct diagnosis states."""

    summary: str
    concepts: tuple[TermGroup, ...]
    role: str
    """The work item the diagnosis must be about (it must cite its issue or PR)."""

    def stating_clause(self, text: str, *, item_numbers: frozenset[int]) -> str:
        """The first clause that names EVERY concept together, or ``""``.

        One clause, not the whole text: naming the label in one place and the
        review in another does not connect them, and contrast words split
        clauses, so "#901 has blocked-failed, but that label does not prevent
        code review" does not qualify. Polarity is deliberately NOT judged:
        the withheld-review concept is itself a negation ("the PR never gets
        its code review"), so a lexical reading cannot tell it from a denial.
        That limit is why the clause is reported as evidence for a human to
        audit rather than trusted silently.

        One polarity IS judged, because it is unambiguous: a negated CAUSAL
        verb ("blocked-failed does not prevent code review") denies the
        relationship itself, whereas a correct diagnosis negates the EFFECT
        ("never gets its code review") and keeps the cause affirmative.
        """
        return next(
            (
                clause
                for clause in _clauses(text)
                if all(group.named_in(clause) for group in self.concepts)
                and _about_item(clause, item_numbers)
                and not _DENIED_CAUSE.search(clause)
            ),
            "",
        )


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

    def __post_init__(self) -> None:
        for what, types in (
            ("right_action_types", self.right_action_types),
            ("acceptable_action_types", self.acceptable_action_types),
            ("forbidden_action_types", self.forbidden_action_types),
        ):
            require_tech_lead_action_types(types, what=f"remedy {what}")
        if self.forbidden_action_types & (self.right_action_types | self.acceptable_action_types):
            raise ValueError("a remedy cannot both allow and forbid the same action type")


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


def pr_checks_green(role: str) -> Goal:
    """The item's latest PR is CI-green: a case whose premise is "green PR"
    must not pass on a red or unverifiable one."""

    def check(item: WorkItemFact) -> GoalCheck:
        # The latest PR, like pr_in_state: a merged green PR is still green;
        # whether its state is acceptable is the state goal's question.
        if not item.pull_requests:
            return GoalCheck(False, f"issue #{item.issue_number} has no pull request")
        latest = max(item.pull_requests, key=lambda pr: pr.number)
        return GoalCheck(latest.checks == "SUCCESS", f"PR #{latest.number} checks: {latest.checks}")

    return Goal(f"{role}.pr_checks_green", role, f"the {role} PR's checks are green", check)


def pr_review_approved(role: str) -> Goal:
    """A review session approved the item's latest PR (not merely a label)."""

    def check(item: WorkItemFact) -> GoalCheck:
        if not item.pull_requests:
            return GoalCheck(False, f"issue #{item.issue_number} has no pull request")
        latest = max(item.pull_requests, key=lambda pr: pr.number)
        approved = latest.number in item.approved_prs
        others = sorted(item.approved_prs - {latest.number})
        detail = f"approved PRs: {others}" if others else "no review.approved for it"
        return GoalCheck(
            approved,
            f"PR #{latest.number} review approved" if approved else f"PR #{latest.number}: {detail}",
        )

    return Goal(f"{role}.pr_review_approved", role, f"a review session approved the {role} PR", check)


def issue_keeps_labels(role: str, labels: Iterable[str]) -> Goal:
    """The issue still carries ``labels`` (e.g. a pause only a human lifts)."""
    wanted = frozenset(labels)

    def check(item: WorkItemFact) -> GoalCheck:
        missing = sorted(wanted - item.issue_labels)
        return GoalCheck(
            not missing,
            f"issue #{item.issue_number} lost {missing}" if missing
            else f"issue #{item.issue_number} still carries {sorted(wanted)}",
        )

    return Goal(f"{role}.keeps_labels", role, f"the {role} issue keeps {sorted(wanted)}", check)


def single_pull_request(role: str) -> Goal:
    """The item has exactly one PR: the work was published once.

    A second PR means the work was redone or republished, and the other
    goals, which read the newest PR, could otherwise pass on it while the
    original work sits stranded.
    """

    def check(item: WorkItemFact) -> GoalCheck:
        numbers = sorted(pr.number for pr in item.pull_requests)
        return GoalCheck(len(numbers) == 1, f"linked PRs: {numbers or '(none)'}")

    return Goal(f"{role}.single_pull_request", role, f"the {role} work is published as exactly one PR", check)


def published_work_survives(role: str) -> Goal:
    """No pull request of the item was closed unmerged or lost its branch."""

    def check(item: WorkItemFact) -> GoalCheck:
        # A merged PR's branch is routinely deleted (GitHub's auto-delete);
        # only an UNmerged PR losing its branch lost work — the same rule
        # the grader's destruction report applies.
        lost = [
            pr.number
            for pr in item.pull_requests
            if pr.state is PullRequestState.CLOSED_UNMERGED
            or (not pr.branch_exists and pr.state is not PullRequestState.MERGED)
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
