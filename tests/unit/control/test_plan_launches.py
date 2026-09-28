"""One launch per subject per plan, and every queue admission through its owner (#7454)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from issue_orchestrator.control.action_base import Action
from issue_orchestrator.control.actions import (
    ActionType,
    AddLabelAction,
    LaunchSessionAction,
    LaunchValidationRetryAction,
    SessionType,
)
from issue_orchestrator.control.plan_launches import PlanLaunches, launch_subject
from issue_orchestrator.control.planner_types import SkippedItem


def test_a_second_launch_of_one_subject_is_refused_across_launch_kinds() -> None:
    """Porchpin: a health-review anchor planned as a tech-lead run AND an issue."""
    skipped: list[SkippedItem] = []
    launches = PlanLaunches(skipped)
    actions: list[Action] = []

    tech_lead = LaunchSessionAction(session_type=SessionType.TECH_LEAD, number=394)
    issue = LaunchSessionAction(session_type=SessionType.ISSUE, number=394)
    other = LaunchSessionAction(session_type=SessionType.ISSUE, number=395)

    assert launches.admit([tech_lead], into=actions) == 1
    assert launches.admit([issue, other], into=actions) == 1

    assert actions == [tech_lead, other]
    assert [(s.number, s.item_type) for s in skipped] == [(394, "issue launch")]
    assert "already has a tech-lead launch" in skipped[0].reason


def test_a_validation_retry_and_an_issue_launch_of_one_issue_are_one_subject() -> None:
    skipped: list[SkippedItem] = []
    launches = PlanLaunches(skipped)
    actions: list[Action] = []

    retry = LaunchValidationRetryAction(issue_number=7, retry_count=1)
    issue = LaunchSessionAction(session_type=SessionType.ISSUE, number=7)

    assert launches.admit([retry, issue], into=actions) == 1
    assert actions == [retry]
    assert [s.number for s in skipped] == [7]


def test_the_same_launch_twice_is_refused() -> None:
    skipped: list[SkippedItem] = []
    launches = PlanLaunches(skipped)
    actions: list[Action] = []
    review = LaunchSessionAction(session_type=SessionType.REVIEW, number=70)

    assert launches.admit([review, review], into=actions) == 1
    assert actions == [review]
    assert [(s.number, s.item_type) for s in skipped] == [(70, "review launch")]


def test_distinct_session_kinds_of_one_issue_that_are_not_new_issue_work_both_launch() -> None:
    """A rework and a tech-lead investigation of one issue are separate sessions.

    The plan-level rule refuses duplicates and new coding work on an issue the
    plan already launches; it does not second-guess the stages that plan two
    different kinds of work for one issue.
    """
    skipped: list[SkippedItem] = []
    launches = PlanLaunches(skipped)
    actions: list[Action] = []
    rework = LaunchSessionAction(session_type=SessionType.REWORK, number=7)
    tech_lead = LaunchSessionAction(session_type=SessionType.TECH_LEAD, number=7)

    assert launches.admit([rework, tech_lead], into=actions) == 2
    assert actions == [rework, tech_lead]
    assert skipped == []


def test_non_launch_actions_pass_through_and_cost_nothing() -> None:
    skipped: list[SkippedItem] = []
    launches = PlanLaunches(skipped)
    actions: list[Action] = []
    label = AddLabelAction(issue_number=7, label="provider-unavailable")

    assert launches.admit([label, label], into=actions) == 0
    assert actions == [label, label]
    assert skipped == []


def test_a_launch_kind_without_a_subject_fails_loudly() -> None:
    with pytest.raises(TypeError, match="names no subject"):
        launch_subject(Action(action_type=ActionType.LAUNCH_VALIDATION_RETRY))


_SRC = Path(__file__).resolve().parents[3] / "src" / "issue_orchestrator"
_QUEUE_OWNER = _SRC / "domain" / "models.py"
_DIRECT_ADMISSION = re.compile(
    r"\.pending_(reviews|reworks|retrospective_reviews|validation_retries|tech_lead_reviews)"
    r"\s*\.\s*(append|extend|insert)\("
)


def test_every_pending_queue_admission_goes_through_its_owner() -> None:
    """A queue's duplicate rule lives in exactly one place.

    Case U: the startup PR scan appended a review with a whole-object ``not
    in`` check, so a review the in-flight ledger had already returned (same
    PR, different fields) went on the queue twice and was planned twice.
    Every producer must admit through ``OrchestratorState.queue_pending_*``
    (or ``PendingSessionQueues`` for tech-lead work), never the list itself.
    """
    offenders = [
        f"{path.relative_to(_SRC)}:{number}: {line.strip()}"
        for path in sorted(_SRC.rglob("*.py"))
        if path != _QUEUE_OWNER
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if _DIRECT_ADMISSION.search(line)
    ]
    assert offenders == []


def _retry(issue_number: int):
    from issue_orchestrator.domain.models import PendingValidationRetry
    from issue_orchestrator.domain.session_kind import SessionKind

    return PendingValidationRetry(
        issue_number=issue_number,
        issue_title="Retry me",
        agent_label="agent:developer",
        worktree_path=f"/tmp/repo-{issue_number}",
        branch_name=f"{issue_number}-retry",
        original_prompt="original task",
        validation_error="dirty worktree",
        validation_error_file=None,
        retry_count=1,
        source_kind=SessionKind.CODE,
        validation_cmd="make test",
    )


def _launched(plan) -> list[tuple[str, int]]:
    return [
        (launch_kind, subject)
        for action in plan.actions
        if (subject := launch_subject(action)) is not None
        for launch_kind in [
            action.session_type.value
            if isinstance(action, LaunchSessionAction)
            else action.action_type.value
        ]
    ]


def test_an_issue_another_stage_launches_costs_the_issue_pipeline_no_slot() -> None:
    """The issue pipeline excludes what the plan already launches BEFORE it picks.

    One worker slot is left after the validation retry of #1. Refusing an
    issue launch of #1 after the scheduler picked it would leave that slot
    empty and #2 unplanned.
    """
    from issue_orchestrator.control.planner import Planner
    from issue_orchestrator.control.scheduler import Scheduler
    from tests.unit.test_planner import make_config, make_issue, make_snapshot

    config = make_config(max_concurrent_sessions=2)
    planner = Planner(config=config, scheduler=Scheduler(config))

    plan = planner.plan(
        make_snapshot(
            issues=[make_issue(1), make_issue(2)],
            pending_validation_retries=[_retry(1)],
        )
    )

    assert _launched(plan) == [("launch_validation_retry", 1), ("issue", 2)]
    assert not any("duplicate launch" in s.reason for s in plan.skipped)


def test_a_queue_holding_one_pr_twice_does_not_crowd_out_the_next_review() -> None:
    from issue_orchestrator.control.planner import Planner
    from issue_orchestrator.control.scheduler import Scheduler
    from issue_orchestrator.control.workflows import ReviewWorkflow
    from issue_orchestrator.domain.issue_key import FakeIssueKey
    from issue_orchestrator.domain.models import PendingReview
    from tests.unit.test_planner import make_config, make_snapshot

    def review(pr: int, agent_label: str | None) -> PendingReview:
        return PendingReview(
            issue_key=FakeIssueKey(name=str(pr - 60)),
            pr_number=pr,
            pr_url="url",
            branch_name="branch",
            _issue_number=pr - 60,
            agent_label=agent_label,
        )

    config = make_config(max_concurrent_sessions=2)
    config.code_review_agent = "agent:developer"
    events = MagicMock()
    planner = Planner(
        config=config,
        scheduler=Scheduler(config),
        review_workflow=ReviewWorkflow(config=config, events=events),
    )

    plan = planner.plan(
        make_snapshot(
            pending_reviews=[review(70, "agent:developer"), review(70, None), review(71, None)]
        )
    )

    assert _launched(plan) == [("review", 70), ("review", 71)]


def test_one_coder_session_per_issue_per_plan_and_the_slot_goes_to_the_next_retry() -> None:
    """A rework and a validation retry of #7 both drive #7's branch.

    The rework stage plans first; the retry of #7 is withheld before the
    validation-retry stage spends capacity, so retry #8 takes the slot.
    """
    from issue_orchestrator.control.planner import Planner
    from issue_orchestrator.control.scheduler import Scheduler
    from issue_orchestrator.control.workflows import ReworkWorkflow
    from issue_orchestrator.domain.issue_key import FakeIssueKey
    from issue_orchestrator.domain.models import PendingRework
    from tests.unit.test_planner import make_config, make_snapshot

    config = make_config(max_concurrent_sessions=2)
    planner = Planner(
        config=config,
        scheduler=Scheduler(config),
        rework_workflow=ReworkWorkflow(config=config, events=MagicMock()),
    )

    plan = planner.plan(
        make_snapshot(
            pending_reworks=[
                PendingRework(
                    issue_key=FakeIssueKey(name="7"), agent_type="agent:developer", issue_number=7
                )
            ],
            pending_validation_retries=[_retry(7), _retry(8)],
        )
    )

    assert _launched(plan) == [("rework", 7), ("launch_validation_retry", 8)]


def test_a_validation_retry_after_a_rework_of_its_issue_is_refused() -> None:
    skipped: list[SkippedItem] = []
    launches = PlanLaunches(skipped)
    actions: list[Action] = []
    rework = LaunchSessionAction(session_type=SessionType.REWORK, number=7)
    retry = LaunchValidationRetryAction(issue_number=7, retry_count=1)

    assert launches.admit([rework, retry], into=actions) == 1
    assert actions == [rework]
    assert launches.coder_subjects() == frozenset({7})


def test_a_review_the_recovery_owner_holds_waits_without_taking_a_slot() -> None:
    """#7455: with one slot, a review held by recovery must not crowd out the
    next review, and planning must not send it to launch (no live reads)."""
    from dataclasses import replace

    from issue_orchestrator.control.planner import Planner
    from issue_orchestrator.control.scheduler import Scheduler
    from issue_orchestrator.control.workflows import ReviewWorkflow
    from issue_orchestrator.domain.issue_key import FakeIssueKey
    from issue_orchestrator.domain.models import PendingReview
    from tests.unit.test_planner import make_config, make_snapshot

    def review(pr: int) -> PendingReview:
        return PendingReview(
            issue_key=FakeIssueKey(name=str(pr - 60)),
            pr_number=pr,
            pr_url="url",
            branch_name="branch",
            _issue_number=pr - 60,
        )

    config = make_config(max_concurrent_sessions=1)
    config.code_review_agent = "agent:developer"
    planner = Planner(
        config=config,
        scheduler=Scheduler(config),
        review_workflow=ReviewWorkflow(config=config, events=MagicMock()),
    )
    snapshot = replace(
        make_snapshot(pending_reviews=[review(70), review(71)]),
        recovery_held_reviews=frozenset({70}),
    )

    plan = planner.plan(snapshot)

    assert _launched(plan) == [("review", 71)]
    assert [(s.number, s.reason) for s in plan.skipped if s.number == 70] == [
        (70, "held_by_recovery")
    ]
