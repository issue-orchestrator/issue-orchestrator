"""One launch per subject per plan, and every queue admission through its owner (#7454)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

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
