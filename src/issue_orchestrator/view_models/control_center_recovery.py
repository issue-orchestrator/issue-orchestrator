"""One strict transport mapper for Control Center retained-work rows."""

from __future__ import annotations

from dataclasses import asdict

from ..contracts.public import (
    ControlCenterRecoveryRowsContract,
    GuardedRecoveryStopActionContract,
    OwnedRecoveryRecordContract,
    RecoveryAuthorityContract,
    RecoveryClaimOwnerContract,
    RecoveryEngineGroupContract,
    RecoveryEngineIdentityContract,
    RecoveryProcessIdentityContract,
    RecoveryRecordFactContract,
    UnownedRecoveryRecordContract,
)
from ..domain.control_center_recovery import (
    ControlCenterRecoveryRows,
    GuardedRecoveryStopAction,
    OwnedRecoveryRecord,
    RecoveryEngineGroup,
    RecoveryRecordFact,
    RecoveryRowsStatus,
    UnownedRecoveryRecord,
)
from ..domain.repository_engine_lifecycle import EngineIdentity
from ..domain.validated_work_claim import ProcessIdentity
from ..domain.validated_work_commands import ValidatedWorkAuthoritySnapshot
from ..domain.validated_work_discovery import ClaimOwnerFact


class ControlCenterRecoveryTransportMapper:
    """Keep wire-shape validation separate from relational projection policy."""

    @staticmethod
    def to_contract(
        rows: ControlCenterRecoveryRows,
    ) -> ControlCenterRecoveryRowsContract:
        if type(rows) is not ControlCenterRecoveryRows:
            raise TypeError("recovery transport requires typed internal rows")
        return ControlCenterRecoveryRowsContract.model_validate(asdict(rows))

    @staticmethod
    def parse_contract(payload: object) -> ControlCenterRecoveryRows:
        contract = ControlCenterRecoveryRowsContract.model_validate(payload).root
        return ControlCenterRecoveryRows(
            repo_key=contract.repo_key,
            status=RecoveryRowsStatus(contract.status),
            engine_groups=tuple(_group(group) for group in contract.engine_groups),
            unowned_records=tuple(_unowned(row) for row in contract.unowned_records),
            message=contract.message,
        )


def _process(contract: RecoveryProcessIdentityContract) -> ProcessIdentity:
    return ProcessIdentity(
        contract.host,
        contract.pid,
        contract.started_at,
        contract.instance_id,
    )


def _engine(contract: RecoveryEngineIdentityContract) -> EngineIdentity:
    return EngineIdentity(
        contract.repo_root,
        contract.instance_id,
        contract.host,
        contract.label,
        _process(contract.process),
    )


class RecoveryEngineIdentityTransportMapper:
    """Own the shared strict transport-to-domain engine identity conversion."""

    @staticmethod
    def parse(contract: RecoveryEngineIdentityContract) -> EngineIdentity:
        if type(contract) is not RecoveryEngineIdentityContract:
            raise TypeError("recovery engine transport must be the strict contract")
        return _engine(contract)


def _authority(
    contract: RecoveryAuthorityContract,
) -> ValidatedWorkAuthoritySnapshot:
    return ValidatedWorkAuthoritySnapshot(
        contract.record_id,
        contract.evidence_id,
        contract.observation_revision,
        contract.validated_head_sha,
        contract.branch_name,
        contract.repo_slug,
        contract.issue_number,
        contract.pr_number,
        contract.expected_remote_head_sha,
        contract.remote_baseline_status,
    )


def _work(contract: RecoveryRecordFactContract) -> RecoveryRecordFact:
    return RecoveryRecordFact(
        _authority(contract.authority),
        contract.state,
        contract.failure,
        contract.reason,
        contract.escrow_retained,
    )


def _owner(contract: RecoveryClaimOwnerContract) -> ClaimOwnerFact:
    return ClaimOwnerFact(
        _engine(contract.engine),
        contract.owner_fence,
        contract.stop_availability,
    )


def _action(
    contract: GuardedRecoveryStopActionContract | None,
) -> GuardedRecoveryStopAction | None:
    if contract is None:
        return None
    return GuardedRecoveryStopAction(
        contract.record_id,
        _engine(contract.expected_engine),
        contract.expected_owner_fence,
        contract.graceful_timeout_seconds,
        contract.force_on_timeout,
    )


def _owned(contract: OwnedRecoveryRecordContract) -> OwnedRecoveryRecord:
    return OwnedRecoveryRecord(
        contract.kind,
        _work(contract.work),
        _owner(contract.owner),
        _action(contract.stop_action),
    )


def _unowned(contract: UnownedRecoveryRecordContract) -> UnownedRecoveryRecord:
    return UnownedRecoveryRecord(contract.kind, _work(contract.work))


def _group(contract: RecoveryEngineGroupContract) -> RecoveryEngineGroup:
    return RecoveryEngineGroup(
        _engine(contract.engine),
        contract.presentation,
        contract.presentation_message,
        tuple(_owned(row) for row in contract.records),
    )
