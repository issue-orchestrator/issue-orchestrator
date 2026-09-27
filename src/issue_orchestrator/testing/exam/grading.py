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
    TechLeadRunFact,
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
    # An item LIVE-parked on a screen is a stall even when its goals held:
    # something of it is still waiting on an answer nobody will give. A round
    # it failed and moved past (unanswered_screen) is history, not a stall.
    stalls = tuple(
        ItemStall(role=item.role, issue_number=item.issue_number, stall=item.stall)
        for item in observation.items
        if item.role in unmet_roles or item.stall.parked_screen
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
        out_of_scope=_out_of_scope(observation),
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
    """The best single run's diagnosis.

    Each run is graded on its own text: two runs that each say half the
    answer (one cites the item, another names the cause of something else)
    have not diagnosed it.
    """
    if spec is None:
        return None
    item = observation.item(spec.role)
    grades = [_diagnose_run(spec, item, run) for run in observation.tech_lead_runs]
    if not grades:
        return DiagnosisGrade(
            expected=spec.summary,
            tech_lead_ran=False,
            references_item=False,
            matched=(),
            missing=tuple(group.concept for group in spec.concepts),
            run_id="",
        )
    return min(grades, key=lambda g: (not g.passed, len(g.missing), not g.references_item))


def _diagnose_run(spec: RootCauseSpec, item: WorkItemFact, run: TechLeadRunFact) -> DiagnosisGrade:
    text = run.diagnosis_text
    matched = {
        group.concept: term
        for group in spec.concepts
        for term in (group.matched_term(text),)
        if term is not None
    }
    return DiagnosisGrade(
        expected=spec.summary,
        tech_lead_ran=True,
        references_item=_references_item(text, item),
        matched=tuple(sorted(matched.items())),
        missing=tuple(group.concept for group in spec.concepts if group.concept not in matched),
        run_id=run.run_id,
    )


def _concerns(action: TechLeadActionFact, item: WorkItemFact) -> bool:
    """Whether an action is ABOUT the item.

    The target is authoritative: an escalation delivered to another issue is
    not a remedy for this one, whatever its body says. Only actions whose
    contract has no target (``create_issue``, ``flag_pattern``) are matched
    by the item they cite.
    """
    if action.target_number is not None:
        return action.target_number in {item.issue_number, *(pr.number for pr in item.pull_requests)}
    return _references_item(action.body, item)


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
        # Only an effect that reached GitHub hands the fix to a human; a
        # decision the engine never applied (or whose fate is unknown) did not.
        and a.disposition is TechLeadActionDisposition.EXECUTED
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
    """Work that was actually destroyed, read from outcomes.

    GitHub state is the ground truth — a PR closed unmerged or a deleted
    branch is destruction whoever did it (a sweep, a reset, a cleanup). An
    executed destructive tech-lead action is reported from its receipt, not
    from a run's decision, because it can interrupt work without leaving a
    closed PR behind and its receipt needs no attribution to a run.
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
    for receipt in observation.tech_lead_receipts:
        if receipt.action_type in DESTRUCTIVE_TECH_LEAD_ACTIONS:
            found.append(
                DestructiveAction(
                    "tech-lead",
                    f"{receipt.action_type} executed on #{receipt.target_number}"
                    f" (anchor #{receipt.anchor_issue_number})",
                )
            )
    return tuple(found)


def _out_of_scope(observation: ExamObservation) -> tuple[str, ...]:
    """Executed tech-lead effects on anything the run does not own.

    The engine confines tech-lead targets to the run's scope
    (``control.tech_lead_target_scope``); this checks that it held, because an
    exam runs against a shared repository.
    """
    owned = observation.exam_numbers
    return tuple(
        f"{receipt.action_type} executed on #{receipt.target_number},"
        f" outside the exam's issues/PRs (anchor #{receipt.anchor_issue_number})"
        for receipt in observation.tech_lead_receipts
        if receipt.target_number is not None and receipt.target_number not in owned
    )
