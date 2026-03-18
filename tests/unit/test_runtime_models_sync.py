from __future__ import annotations

from dataclasses import fields

from issue_orchestrator.domain import models as domain_models
from issue_orchestrator.entrypoints.cli_tools import _runtime_models as runtime_models


def test_runtime_completion_outcome_matches_domain_enum() -> None:
    assert [member.value for member in runtime_models.CompletionOutcome] == [
        member.value for member in domain_models.CompletionOutcome
    ]


def test_runtime_requested_action_matches_domain_enum() -> None:
    assert [member.value for member in runtime_models.RequestedAction] == [
        member.value for member in domain_models.RequestedAction
    ]


def test_runtime_follow_up_issue_fields_match_domain_model() -> None:
    assert [field.name for field in fields(runtime_models.ProposedFollowUpIssue)] == [
        field.name for field in fields(domain_models.ProposedFollowUpIssue)
    ]


def test_runtime_completion_record_fields_match_domain_model() -> None:
    assert [field.name for field in fields(runtime_models.CompletionRecord)] == [
        field.name for field in fields(domain_models.CompletionRecord)
    ]


def test_runtime_completion_record_serialization_matches_domain_model() -> None:
    domain_record = domain_models.CompletionRecord(
        session_id="session-1",
        timestamp="2026-03-18T12:00:00Z",
        outcome=domain_models.CompletionOutcome.COMPLETED,
        summary="done",
        requested_actions=[
            domain_models.RequestedAction.PUSH_BRANCH,
            domain_models.RequestedAction.CREATE_PR,
        ],
        implementation="Implemented the fix",
        problems="None",
        comment_body="Looks good",
        validation_record_path=".issue-orchestrator/validation.json",
        follow_up_issues=[
            domain_models.ProposedFollowUpIssue(
                title="Follow-up",
                reason="Ancillary cleanup",
                evidence="See failing test",
                suggested_labels=["bug", "tests"],
                blocking=False,
            )
        ],
    )
    runtime_record = runtime_models.CompletionRecord(
        session_id="session-1",
        timestamp="2026-03-18T12:00:00Z",
        outcome=runtime_models.CompletionOutcome.COMPLETED,
        summary="done",
        requested_actions=[
            runtime_models.RequestedAction.PUSH_BRANCH,
            runtime_models.RequestedAction.CREATE_PR,
        ],
        implementation="Implemented the fix",
        problems="None",
        comment_body="Looks good",
        validation_record_path=".issue-orchestrator/validation.json",
        follow_up_issues=[
            runtime_models.ProposedFollowUpIssue(
                title="Follow-up",
                reason="Ancillary cleanup",
                evidence="See failing test",
                suggested_labels=["bug", "tests"],
                blocking=False,
            )
        ],
    )

    assert runtime_record.to_dict() == domain_record.to_dict()
