"""Cold retained-work queries owned by the Control Center."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest

from issue_orchestrator.control.control_center_recovery_queries import (
    ControlCenterRecoveryQueries,
    UnknownConfiguredRepositoryError,
)
from issue_orchestrator.domain.control_center_recovery import (
    ConfiguredRepository,
    EnginePresentationFact,
    RecoveryEnginePresentation,
    RecoveryRowsStatus,
)
from issue_orchestrator.domain.repository_engine_lifecycle import (
    EngineIdentity,
    EngineStopAvailability,
)
from issue_orchestrator.domain.validated_work import (
    FinalizationPhase,
    LineageRole,
    RemoteBaselineStatus,
    ValidatedWorkKey,
    ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_claim import ProcessIdentity
from issue_orchestrator.domain.validated_work_commands import (
    AbandonStatus,
    ValidatedWorkDisposition,
)
from issue_orchestrator.domain.validated_work_discovery import (
    ClaimOwnerFact,
    ValidatedWorkDiscovery,
    ValidatedWorkSnapshot,
    WorkDiscoveryStatus,
)


REPOSITORY = ConfiguredRepository("repo-key", "/repo", "owner/repo")


def _engine(
    instance: str = "engine-a", *, availability: EngineStopAvailability
) -> ClaimOwnerFact:
    process = ProcessIdentity(
        "local", 40 + len(instance), f"start-{instance}", instance
    )
    engine = EngineIdentity("/repo", instance, "local", instance, process)
    return ClaimOwnerFact(engine, 3, availability)


def _snapshot(
    issue: int,
    branch: str,
    digit: str,
    *,
    owner: ClaimOwnerFact | None,
) -> ValidatedWorkSnapshot:
    key = ValidatedWorkKey("owner/repo", issue, branch, digit * 40)
    disposition = ValidatedWorkDisposition(
        key.record_id,
        key,
        f"evidence-{digit}",
        ValidatedWorkState.PARKED,
        LineageRole.HEAD,
        "retained",
    )
    return ValidatedWorkSnapshot(
        disposition=disposition,
        record_id=key.record_id,
        validated_head_sha=key.validated_head_sha,
        worktree_head_sha=key.validated_head_sha,
        branch_name=branch,
        expected_remote_head_sha=None,
        remote_baseline_status=RemoteBaselineStatus.UNOBSERVED,
        superseded_evidence_ids=(),
        attached_evidence_ids=(),
        lineage_role=LineageRole.HEAD,
        escrow_retained=True,
        observation_revision=2,
        waits_on_record_id="",
        owner=owner,
        publish_attempts=0,
        finalization_phase=FinalizationPhase.NOT_STARTED,
        updated_at="2026-01-01T00:00:00+00:00",
        can_recover=owner is None,
        can_abandon=owner is None,
        abandon_unavailable=None if owner is None else AbandonStatus.REFUSED_STATE,
    )


@dataclass
class Registry:
    repository: ConfiguredRepository | None = REPOSITORY
    calls: list[str] = field(default_factory=list)

    def resolve(self, repo_key: str) -> ConfiguredRepository | None:
        self.calls.append(repo_key)
        return self.repository


@dataclass
class Records:
    discovery: ValidatedWorkDiscovery
    calls: list[str] = field(default_factory=list)

    def discover_repository(self, repo_root: str) -> ValidatedWorkDiscovery:
        self.calls.append(repo_root)
        return self.discovery

    def snapshot_record(self, repo_root: str, record_id: str):
        raise AssertionError("repository discovery must use the batch snapshot")


@dataclass
class Presentations:
    fact: EnginePresentationFact = EnginePresentationFact(
        RecoveryEnginePresentation.OBSERVED,
        "Exact engine incarnation is observed",
    )
    calls: list[EngineIdentity] = field(default_factory=list)

    def presentation_for(self, engine: EngineIdentity) -> EnginePresentationFact:
        self.calls.append(engine)
        return self.fact


def _queries(discovery: ValidatedWorkDiscovery):
    registry = Registry()
    records = Records(discovery)
    presentations = Presentations()
    return (
        ControlCenterRecoveryQueries(
            repositories=registry,
            records=records,
            engine_presentations=presentations,
        ),
        registry,
        records,
        presentations,
    )


def test_query_resolves_registry_scope_and_groups_each_owner_incarnation_once() -> None:
    owner_a = _engine(availability=EngineStopAvailability.AVAILABLE)
    owner_b = _engine(
        "engine-b", availability=EngineStopAvailability.EXACT_TARGET_UNAVAILABLE
    )
    snapshots = (
        _snapshot(9, "z-last", "3", owner=owner_b),
        _snapshot(7, "b-owned", "2", owner=owner_a),
        _snapshot(7, "a-unowned", "1", owner=None),
    )
    query, registry, records, presentations = _queries(
        ValidatedWorkDiscovery(WorkDiscoveryStatus.AVAILABLE, snapshots, "work found")
    )

    result = query.repository("repo-key")

    assert result.status is RecoveryRowsStatus.AVAILABLE
    assert registry.calls == ["repo-key"]
    assert records.calls == ["/repo"]
    assert [group.engine for group in result.engine_groups] == [
        owner_a.engine,
        owner_b.engine,
    ]
    assert presentations.calls == [owner_a.engine, owner_b.engine]
    assert result.engine_groups[0].records[0].stop_action is not None
    assert result.engine_groups[1].records[0].stop_action is None
    assert result.unowned_records[0].work.authority.issue_number == 7


def test_query_preserves_exact_snapshot_authority_and_presentation() -> None:
    owner = _engine(availability=EngineStopAvailability.AVAILABLE)
    snapshot = _snapshot(7, "feature", "1", owner=owner)
    query, _, _, presentations = _queries(
        ValidatedWorkDiscovery(WorkDiscoveryStatus.AVAILABLE, (snapshot,), "work found")
    )
    presentations.fact = EnginePresentationFact(
        RecoveryEnginePresentation.REPLACED,
        "A replacement incarnation is advertised",
    )

    result = query.repository("repo-key")
    row = result.engine_groups[0].records[0]

    assert row.work.authority == snapshot.authority
    assert row.owner == owner
    assert result.engine_groups[0].presentation is RecoveryEnginePresentation.REPLACED


def test_successful_empty_discovery_is_distinct_from_unavailability() -> None:
    query, _, _, presentations = _queries(
        ValidatedWorkDiscovery(WorkDiscoveryStatus.AVAILABLE, (), "read completed")
    )

    result = query.repository("repo-key")

    assert result.status is RecoveryRowsStatus.EMPTY
    assert result.message == "No preserved validated work"
    assert presentations.calls == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (WorkDiscoveryStatus.DATABASE_ABSENT, RecoveryRowsStatus.DATABASE_ABSENT),
        (WorkDiscoveryStatus.UNREADABLE, RecoveryRowsStatus.UNREADABLE),
        (WorkDiscoveryStatus.UNSUPPORTED_SCHEMA, RecoveryRowsStatus.UNSUPPORTED_SCHEMA),
    ],
)
def test_each_unavailable_discovery_status_remains_distinct(
    source: WorkDiscoveryStatus,
    expected: RecoveryRowsStatus,
) -> None:
    query, _, _, presentations = _queries(
        ValidatedWorkDiscovery(source, (), "durable read unavailable")
    )

    result = query.repository("repo-key")

    assert result.status is expected
    assert result.message == "durable read unavailable"
    assert result.engine_groups == result.unowned_records == ()
    assert presentations.calls == []


def test_unknown_public_key_never_reaches_the_record_reader() -> None:
    query, registry, records, _ = _queries(
        ValidatedWorkDiscovery(WorkDiscoveryStatus.AVAILABLE, (), "read completed")
    )
    registry.repository = None

    with pytest.raises(UnknownConfiguredRepositoryError):
        query.repository("caller-controlled-path")

    assert records.calls == []


@pytest.mark.parametrize("mismatch", ["slug", "root"])
def test_discovered_records_must_match_configured_repository(
    mismatch: str,
) -> None:
    owner = _engine(availability=EngineStopAvailability.AVAILABLE)
    snapshot = _snapshot(7, "feature", "1", owner=owner)
    if mismatch == "slug":
        key = ValidatedWorkKey("other/repo", 7, "feature", "1" * 40)
        disposition = replace(
            snapshot.disposition,
            record_id=key.record_id,
            key=key,
        )
        snapshot = replace(snapshot, disposition=disposition, record_id=key.record_id)
    else:
        foreign_engine = replace(owner.engine, repo_root="/other")
        snapshot = replace(snapshot, owner=replace(owner, engine=foreign_engine))
    query, _, _, _ = _queries(
        ValidatedWorkDiscovery(WorkDiscoveryStatus.AVAILABLE, (snapshot,), "work found")
    )

    with pytest.raises(ValueError, match="configured repository"):
        query.repository("repo-key")
