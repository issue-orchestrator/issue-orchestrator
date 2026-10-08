"""Case J's right answer (#8691), graded on synthetic observations.

The proposal bodies and step markers here are rendered by the engine's own
``follow_through_section`` and ``step_marker``, so the grader and the engine
cannot drift apart on what "the steps ran once" or "the operator's checklist"
mean.
"""

from __future__ import annotations

from dataclasses import replace

from issue_orchestrator.domain.decision_steps import (
    DecisionFollowThrough,
    DecisionStep,
    DecisionStepKind,
    follow_through_section,
    step_marker,
)
from issue_orchestrator.testing.exam import grade
from issue_orchestrator.testing.exam.cases import (
    DECIDED,
    DECISION_MILESTONE,
    DECISION_STEPS_RUN_ON_APPROVAL,
    EXAM_CASE_IDS,
    NOTED,
    SIBLING,
    SUPERSEDED,
    decision_steps_run_on_approval,
)
from issue_orchestrator.testing.exam.observation import BodyRulingFact, DecisionProposalFact, WorkItemFact
from tests.unit.testing.exam.builders import item, observation

DECIDED_N, SIBLING_N, NOTED_N, SUPERSEDED_N, PROPOSAL_N = 920, 921, 922, 923, 930
STEPS = DecisionFollowThrough(
    steps=(
        DecisionStep(DecisionStepKind.SET_MILESTONE, SIBLING_N, milestone=DECISION_MILESTONE),
        DecisionStep(DecisionStepKind.RECORD_RULING, DECIDED_N, text="Delivery plan: slice A only."),
        DecisionStep(DecisionStepKind.RECORD_RULING, NOTED_N, text="The fence is built by the route issue."),
        DecisionStep(DecisionStepKind.CLOSE_SUPERSEDED_PROPOSAL, SUPERSEDED_N),
    ),
    operator_steps=("Raise the CI ceiling in .github/workflows/ci.yml to 15 minutes.",),
)
BODY = f"## Decision for #{DECIDED_N}: build slice A\n{follow_through_section(STEPS, subject=DECIDED_N)}"
RULING = (BodyRulingFact("ds-930-2", "approved_decision", "Delivery plan: slice A only."),)


def _applied(*indices: int, verb: str = "applied") -> tuple[str, ...]:
    return tuple(f"Step {i} {verb}: done\n\n{step_marker(str(PROPOSAL_N), i)}" for i in indices)


def _decided(*, body: str = BODY, comments: tuple[str, ...] = _applied(1, 2, 3, 4),
             labels: tuple[str, ...] = ("agent:exam-coder-asks-delivery",)) -> WorkItemFact:
    return replace(
        item(issue_labels=labels), role=DECIDED, issue_number=DECIDED_N, body_rulings=RULING,
        decision_proposals=(DecisionProposalFact(PROPOSAL_N, "closed", body, comments),),
    )


def _right() -> tuple[WorkItemFact, ...]:
    return (
        _decided(),
        replace(item(), role=SIBLING, issue_number=SIBLING_N, milestone=DECISION_MILESTONE),
        replace(item(), role=NOTED, issue_number=NOTED_N,
                body_rulings=(BodyRulingFact("ds-930-3", "approved_decision", "The fence."),)),
        replace(item(), role=SUPERSEDED, issue_number=SUPERSEDED_N, issue_state="closed"),
    )


def _failed(*items: WorkItemFact) -> set[str]:
    case = decision_steps_run_on_approval(needs_human_label="needs-human")
    obs = replace(observation(case.case_id, items[0]), items=items, owned_numbers=frozenset(range(900, 960)))
    return {goal.name for goal in grade(case, obs).goals if not goal.passed}


def _with(decided: WorkItemFact) -> tuple[WorkItemFact, ...]:
    return (decided, *_right()[1:])


def test_case_j_is_a_new_add_only_case() -> None:
    assert DECISION_STEPS_RUN_ON_APPROVAL in EXAM_CASE_IDS
    assert DECISION_STEPS_RUN_ON_APPROVAL.startswith("J-")


def test_the_right_answer_passes() -> None:
    assert _failed(*_right()) == set()


def test_hand_steps_left_to_the_operator_fail_every_consequence() -> None:
    """The pre-#8691 engine: the decision is approved and applied on the issue,
    but every consequence beyond it is prose the operator never acted on."""
    prose = (f"## Decision for #{DECIDED_N}\n\n### Before you approve\n1. Move #{SIBLING_N} to"
             f" {DECISION_MILESTONE}.\n2. Raise the CI ceiling in ci.yml.")
    failed = _failed(
        _decided(body=prose, comments=()),
        replace(item(), role=SIBLING, issue_number=SIBLING_N, milestone="M0"),
        replace(item(), role=NOTED, issue_number=NOTED_N),
        replace(item(), role=SUPERSEDED, issue_number=SUPERSEDED_N, issue_state="open"),
    )
    assert failed == {
        "decided.decision_steps_ran_once", "decided.operator_checklist", "decided.no_hand_steps_in_prose",
        "sibling.in_milestone", "noted.ruling_in_body", "superseded.issue_closed",
    }


def test_a_step_that_ran_twice_or_was_refused_fails() -> None:
    assert "decided.decision_steps_ran_once" in _failed(*_with(_decided(comments=_applied(1, 2, 3, 3))))
    refused = _applied(1, 2, 3) + _applied(4, verb="not applied")
    assert "decided.decision_steps_ran_once" in _failed(*_with(_decided(comments=refused)))


def test_no_proposal_at_all_fails_the_decision_goals() -> None:
    failed = _failed(*_with(replace(_decided(), decision_proposals=())))
    assert {"decided.decision_steps_ran_once", "decided.no_hand_steps_in_prose"} <= failed


def test_the_item_still_blocked_fails() -> None:
    assert _failed(*_with(_decided(labels=("needs-human",)))) == {"decided.issue_free_of_blocks"}


def test_observation_round_trips_the_new_facts() -> None:
    decided = _decided()
    assert WorkItemFact.from_dict(decided.to_dict()) == decided
