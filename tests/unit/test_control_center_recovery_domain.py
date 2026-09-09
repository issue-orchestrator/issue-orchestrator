"""Relational invariants for the internal Control Center recovery projection."""

from dataclasses import replace

import pytest

from issue_orchestrator.domain.control_center_recovery import (
    ControlCenterRecoveryRows,
    GuardedRecoveryStopAction,
    OwnedRecoveryRecord,
    RecoveryEngineGroup,
    RecoveryEnginePresentation,
    RecoveryRecordFact,
    RecoveryRowsStatus,
    UnownedRecoveryRecord,
)
from issue_orchestrator.domain.repository_engine_lifecycle import (
    EngineIdentity,
    EngineStopAvailability,
)
from issue_orchestrator.domain.validated_work import (
    LineageRole,
    RemoteBaselineStatus,
    ValidatedWorkFailure,
    ValidatedWorkKey,
    ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_claim import ProcessIdentity
from issue_orchestrator.domain.validated_work_commands import (
    ValidatedWorkAuthoritySnapshot,
)
from issue_orchestrator.domain.validated_work_discovery import ClaimOwnerFact


KEY = ValidatedWorkKey("owner/repo", 7, "feature", "1" * 40)
AUTHORITY = ValidatedWorkAuthoritySnapshot(
    KEY.record_id,
    "evidence-1",
    2,
    KEY.validated_head_sha,
    KEY.branch_name,
    KEY.repo_slug,
    KEY.issue_number,
    None,
    None,
    RemoteBaselineStatus.UNOBSERVED,
)
PROCESS = ProcessIdentity("local", 42, "linux-proc-v1:boot:17", "engine-a")
ENGINE = EngineIdentity("/repo", "engine-a", "local", "engine-a", PROCESS)
WORK = RecoveryRecordFact(
    AUTHORITY,
    ValidatedWorkState.PARKED,
    None,
    "retained",
    True,
)
OWNER = ClaimOwnerFact(ENGINE, 3, EngineStopAvailability.AVAILABLE)
ACTION = GuardedRecoveryStopAction(KEY.record_id, ENGINE, 3)
OWNED = OwnedRecoveryRecord("owned", WORK, OWNER, ACTION)
UNOWNED = UnownedRecoveryRecord("unowned", WORK)
GROUP = RecoveryEngineGroup(
    ENGINE,
    RecoveryEnginePresentation.OBSERVED,
    "Exact engine incarnation is observed",
    (OWNED,),
)


def test_rows_require_available_exactly_when_any_rows_exist() -> None:
    available = ControlCenterRecoveryRows(
        "repo-key", RecoveryRowsStatus.AVAILABLE, (GROUP,), (), "work found"
    )
    assert available.engine_groups == (GROUP,)

    with pytest.raises(ValueError, match="only AVAILABLE"):
        replace(available, status=RecoveryRowsStatus.EMPTY)
    with pytest.raises(ValueError, match="only AVAILABLE"):
        ControlCenterRecoveryRows(
            "repo-key", RecoveryRowsStatus.AVAILABLE, (), (), "work found"
        )


def test_rows_reject_duplicate_records_and_engine_groups() -> None:
    with pytest.raises(ValueError, match="exactly once"):
        ControlCenterRecoveryRows(
            "repo-key",
            RecoveryRowsStatus.AVAILABLE,
            (GROUP,),
            (UNOWNED,),
            "work found",
        )
    with pytest.raises(ValueError, match="exactly one group"):
        ControlCenterRecoveryRows(
            "repo-key",
            RecoveryRowsStatus.AVAILABLE,
            (GROUP, GROUP),
            (),
            "work found",
        )


def test_engine_group_requires_nonempty_rows_for_one_exact_incarnation() -> None:
    with pytest.raises(ValueError, match="requires owned records"):
        replace(GROUP, records=())
    other_process = replace(PROCESS, pid=43)
    other_engine = replace(ENGINE, process=other_process)
    other_owner = replace(OWNER, engine=other_engine)
    other_row = OwnedRecoveryRecord(
        "owned",
        WORK,
        other_owner,
        replace(ACTION, expected_engine=other_engine),
    )
    with pytest.raises(ValueError, match="exact engine"):
        replace(GROUP, records=(other_row,))


def test_owned_action_presence_and_identity_follow_owner_capability() -> None:
    with pytest.raises(ValueError, match="availability"):
        replace(OWNED, stop_action=None)
    unavailable = replace(
        OWNER, stop_availability=EngineStopAvailability.EXACT_TARGET_UNAVAILABLE
    )
    assert replace(OWNED, owner=unavailable, stop_action=None).stop_action is None
    with pytest.raises(ValueError, match="exact record, owner and fence"):
        replace(OWNED, stop_action=replace(ACTION, expected_owner_fence=4))


def test_unowned_rows_reject_resolved_work() -> None:
    with pytest.raises(ValueError, match="resolved unowned"):
        replace(UNOWNED, work=replace(WORK, state=ValidatedWorkState.RECOVERED))


def test_recovery_fact_requires_enumerated_failure_for_failed_state() -> None:
    with pytest.raises(ValueError, match="FAILED requires"):
        replace(WORK, state=ValidatedWorkState.FAILED)
    failed = replace(
        WORK,
        state=ValidatedWorkState.FAILED,
        failure=ValidatedWorkFailure.PUSH_FAILED,
    )
    assert failed.failure is ValidatedWorkFailure.PUSH_FAILED


def test_stop_action_rejects_boolean_fence() -> None:
    with pytest.raises(ValueError, match="non-negative integer fence"):
        replace(ACTION, expected_owner_fence=True)


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf")])
def test_stop_action_requires_a_finite_positive_graceful_timeout(
    timeout: object,
) -> None:
    with pytest.raises(ValueError, match="positive finite number"):
        replace(ACTION, graceful_timeout_seconds=timeout)  # type: ignore[arg-type]


def test_stop_action_requires_a_strict_force_policy_boolean() -> None:
    with pytest.raises(ValueError, match="boolean"):
        replace(ACTION, force_on_timeout=1)  # type: ignore[arg-type]
