"""Strict public transport for Control Center retained-work rows."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest
from pydantic import ValidationError

from issue_orchestrator.contracts.public import (
    ControlCenterRecoveryRowsContract,
    generate_public_schemas,
)
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
    RemoteBaselineStatus,
    ValidatedWorkKey,
    ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_claim import ProcessIdentity
from issue_orchestrator.domain.validated_work_commands import (
    ValidatedWorkAuthoritySnapshot,
)
from issue_orchestrator.domain.validated_work_discovery import ClaimOwnerFact
from issue_orchestrator.view_models.control_center_recovery import (
    ControlCenterRecoveryTransportMapper,
)


def _work(issue: int, branch: str, digit: str) -> RecoveryRecordFact:
    key = ValidatedWorkKey("owner/repo", issue, branch, digit * 40)
    authority = ValidatedWorkAuthoritySnapshot(
        key.record_id,
        f"evidence-{digit}",
        2,
        key.validated_head_sha,
        key.branch_name,
        key.repo_slug,
        key.issue_number,
        None,
        None,
        RemoteBaselineStatus.UNOBSERVED,
    )
    return RecoveryRecordFact(
        authority,
        ValidatedWorkState.PARKED,
        None,
        "",
        True,
    )


def _rows() -> ControlCenterRecoveryRows:
    process = ProcessIdentity("local", 42, "linux-proc-v1:boot:17", "engine-a")
    engine = EngineIdentity("/repo", "engine-a", "local", "Engine A", process)
    owner = ClaimOwnerFact(engine, 3, EngineStopAvailability.AVAILABLE)
    owned_work = _work(7, "owned", "1")
    owned = OwnedRecoveryRecord(
        "owned",
        owned_work,
        owner,
        GuardedRecoveryStopAction(owned_work.authority.record_id, engine, 3),
    )
    group = RecoveryEngineGroup(
        engine,
        RecoveryEnginePresentation.OBSERVED,
        "Exact engine incarnation is observed",
        (owned,),
    )
    return ControlCenterRecoveryRows(
        "repo-key",
        RecoveryRowsStatus.AVAILABLE,
        (group,),
        (UnownedRecoveryRecord("unowned", _work(8, "unowned", "2")),),
        "Preserved validated work is available",
    )


def _payload() -> dict[str, object]:
    return ControlCenterRecoveryTransportMapper.to_contract(_rows()).model_dump(
        mode="json"
    )


def test_transport_round_trips_exact_internal_projection_without_root_wrapper() -> None:
    rows = _rows()

    contract = ControlCenterRecoveryTransportMapper.to_contract(rows)
    payload = contract.model_dump(mode="json")

    assert "root" not in payload
    assert payload["status"] == "available"
    assert payload["engine_groups"][0]["records"][0]["stop_action"] == {
        "record_id": rows.engine_groups[0].records[0].work.authority.record_id,
        "expected_engine": payload["engine_groups"][0]["engine"],
        "expected_owner_fence": 3,
        "graceful_timeout_seconds": 120.0,
        "force_on_timeout": True,
    }
    assert ControlCenterRecoveryTransportMapper.parse_contract(payload) == rows


@pytest.mark.parametrize(
    "status",
    [
        RecoveryRowsStatus.EMPTY,
        RecoveryRowsStatus.DATABASE_ABSENT,
        RecoveryRowsStatus.UNREADABLE,
        RecoveryRowsStatus.UNSUPPORTED_SCHEMA,
    ],
)
def test_empty_and_unavailable_variants_round_trip_without_rows(
    status: RecoveryRowsStatus,
) -> None:
    rows = ControlCenterRecoveryRows("repo-key", status, (), (), "visible status")

    payload = ControlCenterRecoveryTransportMapper.to_contract(rows).model_dump(
        mode="json"
    )

    assert payload["status"] == status.value
    assert ControlCenterRecoveryTransportMapper.parse_contract(payload) == rows


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("engine_groups", 0),
        ("engine_groups", 0, "engine"),
        ("engine_groups", 0, "engine", "process"),
        ("engine_groups", 0, "records", 0),
        ("engine_groups", 0, "records", 0, "work"),
        ("engine_groups", 0, "records", 0, "work", "authority"),
        ("engine_groups", 0, "records", 0, "owner"),
        ("engine_groups", 0, "records", 0, "stop_action"),
        ("unowned_records", 0),
    ],
)
def test_every_recovery_object_rejects_unknown_fields(path: tuple[object, ...]) -> None:
    payload = _payload()
    target: object = payload
    for part in path:
        target = target[part]  # type: ignore[index]
    assert isinstance(target, dict)
    target["unexpected"] = "smuggled"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ControlCenterRecoveryRowsContract.model_validate(payload)


def test_unowned_variant_cannot_smuggle_owner_or_stop_action() -> None:
    payload = _payload()
    unowned = payload["unowned_records"][0]  # type: ignore[index]
    owner = payload["engine_groups"][0]["records"][0]["owner"]  # type: ignore[index]
    assert isinstance(unowned, dict)
    unowned["owner"] = owner
    unowned["stop_action"] = None

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ControlCenterRecoveryRowsContract.model_validate(payload)


def test_owned_variant_requires_explicit_nullable_stop_action() -> None:
    payload = _payload()
    owned = payload["engine_groups"][0]["records"][0]  # type: ignore[index]
    assert isinstance(owned, dict)
    del owned["stop_action"]

    with pytest.raises(ValidationError, match="Field required"):
        ControlCenterRecoveryRowsContract.model_validate(payload)


def test_parse_reapplies_relational_action_and_collection_invariants() -> None:
    payload = _payload()
    action = payload["engine_groups"][0]["records"][0]["stop_action"]  # type: ignore[index]
    assert isinstance(action, dict)
    action["expected_owner_fence"] = 4
    with pytest.raises(ValueError, match="exact record, owner and fence"):
        ControlCenterRecoveryTransportMapper.parse_contract(payload)

    duplicate = _payload()
    duplicate["engine_groups"].append(  # type: ignore[union-attr]
        deepcopy(duplicate["engine_groups"][0])  # type: ignore[index]
    )
    with pytest.raises(ValueError, match="exactly one group"):
        ControlCenterRecoveryTransportMapper.parse_contract(duplicate)

    no_rows = _payload()
    no_rows["engine_groups"] = []
    no_rows["unowned_records"] = []
    with pytest.raises(ValueError, match="only AVAILABLE"):
        ControlCenterRecoveryTransportMapper.parse_contract(no_rows)


def test_empty_and_unavailable_transport_variants_reject_rows() -> None:
    for status in ("empty", "database_absent", "unreadable", "unsupported_schema"):
        payload = _payload()
        payload["status"] = status
        with pytest.raises(ValidationError, match="at most 0 items"):
            ControlCenterRecoveryRowsContract.model_validate(payload)


def test_transport_rejects_coercible_authority_before_domain_construction() -> None:
    payload = _payload()
    payload["engine_groups"][0]["records"][0]["owner"][  # type: ignore[index]
        "owner_fence"
    ] = True

    with pytest.raises(ValidationError, match="valid integer"):
        ControlCenterRecoveryRowsContract.model_validate(payload)

    payload = _payload()
    payload["engine_groups"][0]["records"][0]["work"][  # type: ignore[index]
        "escrow_retained"
    ] = 1
    with pytest.raises(ValidationError, match="valid boolean"):
        ControlCenterRecoveryRowsContract.model_validate(payload)


def test_public_schema_is_a_closed_discriminated_recovery_family() -> None:
    schema = generate_public_schemas()["control_center.validated_work"]

    assert len(schema["oneOf"]) == 3
    for definition in schema["$defs"].values():
        if isinstance(definition, dict) and definition.get("type") == "object":
            assert definition.get("additionalProperties") is False
    available = schema["$defs"]["RecoveryAvailableContract"]
    assert set(available["required"]) == {
        "repo_key",
        "status",
        "engine_groups",
        "unowned_records",
        "message",
    }
    assert (
        schema["$defs"]["RecoveryEngineGroupContract"]["properties"]["records"][
            "minItems"
        ]
        == 1
    )
    for name in ("RecoveryEmptyContract", "RecoveryUnavailableContract"):
        properties = schema["$defs"][name]["properties"]
        assert properties["engine_groups"]["maxItems"] == 0
        assert properties["unowned_records"]["maxItems"] == 0


def test_mapper_requires_typed_internal_rows() -> None:
    with pytest.raises(TypeError, match="typed internal rows"):
        ControlCenterRecoveryTransportMapper.to_contract({})  # type: ignore[arg-type]
