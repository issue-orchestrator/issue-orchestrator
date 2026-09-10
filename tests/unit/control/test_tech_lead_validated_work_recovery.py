"""Launch-bound tech-lead recovery uses the shared validated-work owner."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.actions import RecoverValidatedWorkAction
from issue_orchestrator.control.action_results import ActionResultType
from issue_orchestrator.control.recovery_drain import RecoveryDrain
from issue_orchestrator.control.recovery_record_operation import RecoveryRecordOperation
from issue_orchestrator.control.tech_lead_validated_work_recovery import (
    TechLeadValidatedWorkRecoveryExecutor,
)
from issue_orchestrator.control.validated_work_recovery_authority import (
    ValidatedWorkRecoveryAuthority,
)
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.domain.published_work_finalization import PublishedWorkTarget
from issue_orchestrator.domain.recovery_attempt import (
    RecoveryAttemptPending,
    RecoveryAuthorityStale,
)
from issue_orchestrator.domain.recovery_completion import RecoveryCompleted
from issue_orchestrator.domain.validated_work import (
    ReviewDisposition,
    RemoteBaselineStatus,
    ValidatedWorkKey,
    ValidatedWorkFailure,
    ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_commands import (
    DispositionInitiator,
    StoredEvidenceCommand,
)
from issue_orchestrator.domain.recovery_entry import RecoveryRecordRequest
from tests.unit.validated_work_support import Rig, capture


def _authority(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    admission = capture(issue=42, state=ValidatedWorkState.PARKED)
    store.admit(admission)
    evidence = store.evidence_for_id(admission.evidence.evidence_id)
    assert evidence is not None
    return store, evidence.evidence.authority


def _action(authority):
    return RecoverValidatedWorkAction(
        authority=authority,
        rationale="publish the retained validated head",
        proposal_id="A3",
        finding_ids=("F1",),
        anchor_issue_number=900,
    )


def test_launch_selector_grants_only_one_unambiguous_parked_head(tmp_path):
    store, authority = _authority(tmp_path)
    store.admit(capture(issue=43, state=ValidatedWorkState.QUEUED))
    store.admit(
        capture(
            issue=44,
            state=ValidatedWorkState.FAILED,
            failure=ValidatedWorkFailure.PUSH_FAILED,
        )
    )

    grants = ValidatedWorkRecoveryAuthority(store).grants_for((44, 42, 43, 42))

    assert grants == (authority,)


def test_launch_selector_refuses_to_choose_between_two_branch_heads(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    store.admit(capture(issue=42, branch="first", state=ValidatedWorkState.PARKED))
    store.admit(capture(issue=42, branch="second", state=ValidatedWorkState.PARKED))

    assert ValidatedWorkRecoveryAuthority(store).grants_for((42,)) == ()


def test_executor_translates_exact_grant_into_shared_owner_command(tmp_path):
    _store, authority = _authority(tmp_path)
    target = PublishedWorkTarget(
        ValidatedWorkKey(
            authority.repo_slug,
            authority.issue_number,
            authority.branch_name,
            authority.validated_head_sha,
        ),
        81,
        "https://github.com/owner/repo/pull/81",
        ReviewDisposition.ROUTE_TO_PR_REVIEW,
    )
    observed: list[StoredEvidenceCommand] = []

    def recover(command):
        observed.append(command)
        return RecoveryCompleted(target)

    events = Mock()
    result = TechLeadValidatedWorkRecoveryExecutor(events, lambda _command: None, recover).apply(
        _action(authority)
    )

    assert result.success
    assert result.details["terminal_disposition_satisfied"] is True
    assert observed == [
        StoredEvidenceCommand(
            issue_number=42,
            reason="publish the retained validated head",
            initiator=DispositionInitiator.TECH_LEAD,
            evidence_id=authority.evidence_id,
            actor="tech-lead:A3",
            authority=authority,
        )
    ]
    events.publish.assert_called_once()


def test_executor_downgrades_stale_authority_without_reporting_success(tmp_path):
    _store, authority = _authority(tmp_path)
    current = replace(authority, observation_revision=authority.observation_revision + 1)
    stale = RecoveryAuthorityStale(authority, current)
    events = Mock()
    recover = Mock()
    executor = TechLeadValidatedWorkRecoveryExecutor(
        events,
        lambda _command: RecoveryAttemptPending(
            stale.describe(),
            ValidatedWorkFailure.AUTHORITY_SNAPSHOT_STALE,
            stale,
        ),
        recover,
    )

    result = executor.apply(_action(authority))

    assert result.result_type is ActionResultType.SKIPPED
    assert result.details["mode"] == "stale_downgrade"
    assert result.details["authority_stale_fields"] == ["observation_revision"]
    recover.assert_not_called()
    events.publish.assert_called_once()


def test_recovery_drain_forwards_the_exact_approval_to_record_owner(tmp_path):
    _store, authority = _authority(tmp_path)
    operation = Mock()
    pending = RecoveryAttemptPending("still retained")
    operation.run.return_value = pending
    operation.preflight.return_value = None
    drain = RecoveryDrain(
        queue=Mock(),
        operation=operation,
        authority_refresh=Mock(),
        batch_size=1,
        interval_seconds=1,
    )
    command = StoredEvidenceCommand(
        issue_number=42,
        reason="approved recovery",
        initiator=DispositionInitiator.TECH_LEAD,
        evidence_id=authority.evidence_id,
        actor="proposal:123",
        authority=authority,
    )
    state = OrchestratorState()

    assert drain.recover(command, state) is pending
    operation.run.assert_called_once_with(
        RecoveryRecordRequest(
            record_id=authority.record_id,
            evidence_id=authority.evidence_id,
            approved=authority,
        ),
        state,
    )


def test_unobserved_remote_is_not_an_executable_launch_target(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    store.admit(
        capture(
            issue=45,
            state=ValidatedWorkState.PARKED,
            failure=ValidatedWorkFailure.REMOTE_UNREADABLE,
            remote_status=RemoteBaselineStatus.UNOBSERVED,
        )
    )

    assert ValidatedWorkRecoveryAuthority(store).grants_for((45,)) == ()


def test_shared_preflight_reports_exact_changed_authority_without_claim(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    admission = capture(issue=42, state=ValidatedWorkState.PARKED)
    store.admit(admission)
    approved = store.record_for_id(admission.evidence.record_id).current_evidence.authority
    changed = replace(
        admission,
        evidence=replace(
            admission.evidence,
            observations=replace(
                admission.evidence.observations,
                expected_remote_head_sha="c" * 40,
                pr_number=92,
            ),
        ),
    )
    store.admit(changed)
    current = store.record_for_id(admission.evidence.record_id).current_evidence.authority
    execution = Mock()
    operation = RecoveryRecordOperation(
        execution=execution,
        store=store,
        preparation=Mock(),
        publication=Mock(),
        completion=Mock(),
    )
    drain = RecoveryDrain(
        queue=Mock(),
        operation=operation,
        authority_refresh=Mock(),
        batch_size=1,
        interval_seconds=1,
    )
    command = StoredEvidenceCommand(
        issue_number=42,
        reason="approved recovery",
        initiator=DispositionInitiator.TECH_LEAD,
        evidence_id=approved.evidence_id,
        actor="proposal:123",
        authority=approved,
    )

    pending = drain.preflight(command)

    assert pending is not None
    assert pending.failure is ValidatedWorkFailure.AUTHORITY_SNAPSHOT_STALE
    assert pending.authority_stale == RecoveryAuthorityStale(approved, current)
    assert pending.authority_stale.differences() == {
        "observation_revision": {
            "approved": approved.observation_revision,
            "current": current.observation_revision,
        },
        "pr_number": {"approved": 91, "current": 92},
        "expected_remote_head_sha": {
            "approved": approved.expected_remote_head_sha,
            "current": "c" * 40,
        },
    }
    execution.try_enter.assert_not_called()


@pytest.mark.parametrize(
    ("changed_fact", "expected_field"),
    [
        ("pr", "pr_number"),
        ("remote", "expected_remote_head_sha"),
        ("revision", "observation_revision"),
        ("evidence", "evidence_id"),
    ],
)
def test_shared_preflight_names_each_changed_approval_fact(
    tmp_path, changed_fact, expected_field
):
    store = Rig(tmp_path / "work.sqlite").open()
    admission = capture(issue=42, state=ValidatedWorkState.PARKED)
    store.admit(admission)
    approved = store.record_for_id(admission.evidence.record_id).current_evidence.authority
    if changed_fact == "evidence":
        changed = capture(
            issue=42,
            run="run-2",
            state=ValidatedWorkState.PARKED,
            at="2026-09-08T01:00:00Z",
        )
    else:
        observation_changes = {
            "pr": {"pr_number": 92},
            "remote": {"expected_remote_head_sha": "c" * 40},
            "revision": {"observed_blocking_labels": ("new-observation",)},
        }[changed_fact]
        changed = replace(
            admission,
            evidence=replace(
                admission.evidence,
                observations=replace(
                    admission.evidence.observations, **observation_changes
                ),
            ),
        )
    store.admit(changed)
    current = store.record_for_id(admission.evidence.record_id).current_evidence.authority
    operation = RecoveryRecordOperation(
        execution=Mock(),
        store=store,
        preparation=Mock(),
        publication=Mock(),
        completion=Mock(),
    )
    command = StoredEvidenceCommand(
        issue_number=42,
        reason="approved recovery",
        initiator=DispositionInitiator.TECH_LEAD,
        evidence_id=approved.evidence_id,
        actor="proposal:123",
        authority=approved,
    )

    pending = operation.preflight(
        RecoveryRecordRequest(
            record_id=command.authority.record_id,
            evidence_id=command.evidence_id,
            approved=command.authority,
        )
    )

    assert pending is not None and pending.authority_stale is not None
    difference = pending.authority_stale.differences()[expected_field]
    assert difference == {
        "approved": approved.to_dict()[expected_field],
        "current": current.to_dict()[expected_field],
    }
