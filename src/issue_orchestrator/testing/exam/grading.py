"""Grade an exam observation against its case's known right answer."""

from __future__ import annotations

import re
from typing import Iterable

from .case import DESTRUCTIVE_TECH_LEAD_ACTIONS, ExamCase, RemedySpec, RootCauseSpec
from .observation import (
    ExamObservation,
    PullRequestState,
    TechLeadActionDisposition,
    TechLeadActionFact,
    WorkItemFact,
)
from .scorecard import (
    DestructiveAction,
    DiagnosisGrade,
    GoalResult,
    ItemStall,
    RemedyGrade,
    RemedyVerdict,
    Scorecard,
)


def grade(case: ExamCase, observation: ExamObservation) -> Scorecard:
    if observation.case_id != case.case_id:
        raise ValueError(
            f"observation is for case {observation.case_id!r}, not {case.case_id!r}"
        )
    goals = tuple(
        GoalResult(goal.name, goal.role, goal.description, check.passed, check.evidence)
        for goal in case.goals
        for check in (goal.evaluate(observation),)
    )
    unmet_roles = {result.role for result in goals if not result.passed}
    stalls = tuple(
        ItemStall(role=item.role, issue_number=item.issue_number, stall=item.stall)
        for item in observation.items
        if item.role in unmet_roles
    )
    return Scorecard(
        case_id=case.case_id,
        title=case.title,
        fault=case.fault,
        known_blockers=case.known_blockers,
        engine_commit=observation.engine_commit,
        ended_by=observation.ended_by,
        elapsed_seconds=observation.elapsed_seconds,
        goals=goals,
        diagnosis=_grade_diagnosis(case.root_cause, observation),
        remedy=_grade_remedy(case.remedy, observation),
        destructive=_destructive_actions(observation),
        expects_destructive=case.expects_destructive,
        github_calls=observation.github_calls,
        stalls=stalls,
        tech_lead_runs=observation.tech_lead_runs,
        notes=observation.notes,
    )


def _references_item(text: str, item: WorkItemFact) -> bool:
    numbers = {item.issue_number, *(pr.number for pr in item.pull_requests)}
    return any(re.search(rf"#{number}(?!\d)", text) for number in numbers)


def _grade_diagnosis(
    spec: RootCauseSpec | None, observation: ExamObservation
) -> DiagnosisGrade | None:
    if spec is None:
        return None
    item = observation.item(spec.role)
    text = "\n".join(run.diagnosis_text for run in observation.tech_lead_runs)
    matched = {
        group.concept: term
        for group in spec.concepts
        for term in (group.matched_term(text),)
        if term is not None
    }
    missing = tuple(group.concept for group in spec.concepts if group.concept not in matched)
    return DiagnosisGrade(
        expected=spec.summary,
        tech_lead_ran=bool(observation.tech_lead_runs),
        references_item=_references_item(text, item),
        matched=tuple(sorted(matched.items())),
        missing=missing,
    )


def _concerns(action: TechLeadActionFact, item: WorkItemFact) -> bool:
    numbers = {item.issue_number, *(pr.number for pr in item.pull_requests)}
    return action.target_number in numbers or _references_item(action.body, item)


def _grade_remedy(
    spec: RemedySpec | None, observation: ExamObservation
) -> RemedyGrade | None:
    if spec is None:
        return None
    item = observation.item(spec.role)
    actions = [
        action
        for run in observation.tech_lead_runs
        for action in run.actions
        if _concerns(action, item)
    ]
    forbidden = [a for a in actions if a.action_type in spec.forbidden_action_types]
    if forbidden:
        return RemedyGrade(
            expected=spec.summary,
            verdict=RemedyVerdict.WRONG,
            evidence=_describe(forbidden),
            vocabulary_gap=not spec.right_action_types,
        )
    right = [a for a in actions if a.action_type in spec.right_action_types]
    if right:
        return RemedyGrade(spec.summary, RemedyVerdict.RIGHT, _describe(right), False)
    acceptable = [
        a
        for a in actions
        if a.action_type in spec.acceptable_action_types
        and a.disposition is not TechLeadActionDisposition.REJECTED
        and all(group.matched_term(a.body) for group in spec.rationale)
    ]
    if acceptable:
        return RemedyGrade(
            spec.summary,
            RemedyVerdict.ACCEPTABLE,
            _describe(acceptable),
            vocabulary_gap=not spec.right_action_types,
        )
    return RemedyGrade(
        spec.summary,
        RemedyVerdict.MISSING,
        _describe(actions) if actions else "no tech-lead action concerned this item",
        vocabulary_gap=not spec.right_action_types,
    )


def _describe(actions: Iterable[TechLeadActionFact]) -> str:
    return "; ".join(
        f"{a.action_type}->#{a.target_number} ({a.disposition.value})" for a in actions
    )


def _destructive_actions(observation: ExamObservation) -> tuple[DestructiveAction, ...]:
    """Work that was actually destroyed, read from outcomes first.

    GitHub state is the ground truth — a PR closed unmerged or a deleted
    branch is destruction whoever did it (a sweep, a reset, a cleanup). An
    executed destructive tech-lead action is reported too, because it can
    interrupt work without leaving a closed PR behind.
    """
    found: list[DestructiveAction] = []
    for item in observation.items:
        for pr in item.pull_requests:
            if pr.state is PullRequestState.CLOSED_UNMERGED:
                found.append(DestructiveAction(item.role, f"PR #{pr.number} closed unmerged"))
            elif not pr.branch_exists and pr.state is not PullRequestState.MERGED:
                found.append(
                    DestructiveAction(item.role, f"PR #{pr.number} branch {pr.branch} deleted")
                )
    for run in observation.tech_lead_runs:
        for action in run.actions:
            if (
                action.action_type in DESTRUCTIVE_TECH_LEAD_ACTIONS
                and action.disposition is TechLeadActionDisposition.EXECUTED
            ):
                found.append(
                    DestructiveAction(
                        "tech-lead",
                        f"{action.action_type} executed on #{action.target_number}"
                        f" (run {run.run_id})",
                    )
                )
    return tuple(found)
