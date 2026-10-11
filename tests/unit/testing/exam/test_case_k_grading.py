"""Case K's right answer (#8144), graded on synthetic observations.

The delivery PR's listed numbers are parsed from a body the engine's own
``render_delivery_body`` renders, so the grader and the engine cannot drift
apart on what "the delivery PR lists it" means.
"""

from __future__ import annotations

from dataclasses import replace

from issue_orchestrator.domain.integration_branch import MergedIntoBranch, render_delivery_body
from issue_orchestrator.testing.exam import grade
from issue_orchestrator.testing.exam.cases import (
    EXAM_CASE_IDS,
    INTEGRATION_BRANCH_LANDS_APPROVED_WORK,
    LANDED_FIRST,
    LANDED_SECOND,
    LEFT_BEHIND,
    integration_branch_lands_approved_work,
)
from issue_orchestrator.testing.exam.observation import (
    DeliveryPullRequestFact,
    ExamObservation,
    PullRequestState,
    WorkItemFact,
)
from tests.unit.testing.exam.builders import item, observation, pr

APPLIED = "integration.step_applied"
ITEMS = {LANDED_FIRST: (950, 951), LANDED_SECOND: (952, 953), LEFT_BEHIND: (954, 955)}
BODY = render_delivery_body(
    head="exam-integration-x", base="main", tip="f" * 40, ahead_by=6, complete=True,
    merged=tuple(
        MergedIntoBranch(number=pr_number, title=f"Exam {role}", url="u", merge_commit_sha=f"{pr_number:040x}")
        for role, (_, pr_number) in ITEMS.items()
    ),
)


def _item(role: str, *, events: tuple[str, ...] = (APPLIED,), labels: tuple[str, ...] = ("code-reviewed",),
          state: PullRequestState = PullRequestState.MERGED, issue_state: str = "closed") -> WorkItemFact:
    issue_number, pr_number = ITEMS[role]
    return replace(
        item(prs=(pr(state=state, labels=labels, number=pr_number),), events=events),
        role=role, issue_number=issue_number, issue_state=issue_state,
    )


def _right() -> dict[str, WorkItemFact]:
    return {
        LANDED_FIRST: _item(LANDED_FIRST),
        LANDED_SECOND: _item(LANDED_SECOND, events=(APPLIED, APPLIED)),
        LEFT_BEHIND: _item(LEFT_BEHIND, events=(APPLIED, APPLIED, APPLIED)),
    }


def _delivery(body: str = BODY, state: PullRequestState = PullRequestState.READY) -> DeliveryPullRequestFact:
    return DeliveryPullRequestFact(
        number=960, head="exam-integration-x", base="main", state=state,
        listed_pr_numbers=DeliveryPullRequestFact.listed_in(body),
    )


def _failed(items: dict[str, WorkItemFact] | None = None, delivery: DeliveryPullRequestFact | None = None,
            *, no_delivery: bool = False) -> set[str]:
    case = integration_branch_lands_approved_work(
        needs_human_label="needs-human", rework_labels=("needs-rework", "rework-cycle-1"),
    )
    chosen = items or _right()
    obs = replace(
        observation(case.case_id, chosen[LANDED_FIRST]), items=tuple(chosen.values()),
        owned_numbers=frozenset(range(940, 970)),
        delivery=None if no_delivery else (delivery or _delivery()),
    )
    return {goal.name for goal in grade(case, obs).goals if not goal.passed}


def test_case_k_is_an_exam_case() -> None:
    assert INTEGRATION_BRANCH_LANDS_APPROVED_WORK in EXAM_CASE_IDS


def test_the_right_answer_passes() -> None:
    assert _failed() == set()


def test_the_delivery_body_lists_what_the_engine_renders() -> None:
    assert DeliveryPullRequestFact.listed_in(BODY) == (951, 953, 955)


def test_an_unmerged_pr_fails_its_role() -> None:
    items = _right()
    items[LANDED_SECOND] = _item(LANDED_SECOND, state=PullRequestState.READY, issue_state="open")
    assert _failed(items) == {f"{LANDED_SECOND}.pr_merged", f"{LANDED_SECOND}.issue_closed"}


def test_a_rework_of_the_left_behind_pr_fails() -> None:
    items = _right()
    items[LEFT_BEHIND] = _item(LEFT_BEHIND, events=(APPLIED, "review.rework_started", APPLIED),
                               labels=("code-reviewed", "rework-cycle-1"))
    assert _failed(items) == {f"{LEFT_BEHIND}.no_rework"}


def test_a_left_behind_pr_merged_without_a_mechanical_update_fails() -> None:
    items = _right()
    items[LEFT_BEHIND] = _item(LEFT_BEHIND, events=(APPLIED,))
    assert _failed(items) == {f"{LEFT_BEHIND}.integration_steps_applied"}


def test_the_delivery_pr_must_list_every_pr_and_be_open() -> None:
    partial = BODY.replace("- #955 ", "- #999 ")
    assert _failed(delivery=_delivery(partial)) == {"delivery_lists_every_pr"}
    assert _failed(delivery=_delivery(state=PullRequestState.MERGED)) == {"delivery_lists_every_pr"}
    assert _failed(no_delivery=True) == {"delivery_lists_every_pr"}


def test_the_delivery_fact_round_trips_and_old_observations_still_load() -> None:
    case = integration_branch_lands_approved_work(needs_human_label="needs-human", rework_labels=())
    obs = replace(observation(case.case_id, _right()[LANDED_FIRST]), delivery=_delivery())
    assert ExamObservation.from_dict(obs.to_dict()) == obs

    old = obs.to_dict()
    del old["delivery"]
    assert ExamObservation.from_dict(old).delivery is None
