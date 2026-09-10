"""Constructor and identity invariants of bounded disposition slices 1 + 1a."""

from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
import json
import pickle
import sqlite3
from threading import Barrier

import pytest

from issue_orchestrator.domain.models import RequestedAction
from issue_orchestrator.domain.validated_work import (
    AdmittedArtifact,
    ArtifactSlot,
    RESOLVED_STATES,
    UNRESOLVED_STATES,
    ReviewDisposition,
    RemoteBaselineStatus,
    ValidatedWorkFailure,
    ValidatedWorkState,
    canonical_json,
)
from issue_orchestrator.infra.validated_work_codec import decode_evidence
from issue_orchestrator.infra.sqlite_connection import open_sqlite
from issue_orchestrator.infra.validated_work_migrations import (
    migrate_remote_baseline_authority,
)
from issue_orchestrator.domain.validated_work_claim import (
    ClaimSecret,
    ValidatedWorkClaim,
)
from issue_orchestrator.domain.validated_work_gate import DispositionGate, GateSource
from issue_orchestrator.domain.validated_work_commands import (
    DispositionInitiator,
    OperatorResolution,
    StoredEvidenceCommand,
    ValidatedWorkDispositionBatch,
)
from tests.unit.validated_work_support import (
    AT,
    L,
    OWNER,
    Rig,
    capture,
    changed_observations,
)


@pytest.mark.parametrize(
    "state,failure",
    [
        ("failed", ValidatedWorkFailure.PUSH_FAILED),
        (ValidatedWorkState.PUBLISHING, None),
        (ValidatedWorkState.RECOVERED, None),
        (ValidatedWorkState.ABANDONED, None),
        (ValidatedWorkState.FAILED, None),
        (ValidatedWorkState.FAILED, "push_failed"),
    ],
)
def test_disposition_gate_rejects_invalid_typed_state_and_failure(state, failure):
    with pytest.raises(ValueError):
        DispositionGate(state, failure, "gate")


def test_gate_source_distinguishes_failure_authority_from_lineage_restriction():
    failure = ValidatedWorkFailure.REMOTE_BASELINE_UNPROVEN
    durable = DispositionGate(ValidatedWorkState.FAILED, failure, "failed")
    derived = DispositionGate(ValidatedWorkState.PARKED, failure, "waiting")
    admission = capture(state=ValidatedWorkState.PARKED, reason="operator approval")
    base = DispositionGate(
        admission.initial_state, admission.initial_failure, admission.initial_reason
    )
    assert durable.source is GateSource.DURABLE_FAILURE
    assert durable.restore(base) == durable
    assert not durable.tracks(base)
    assert derived.source is GateSource.LINEAGE_RESTRICTION
    assert derived.tracks(base)
    restored = derived.restore(base)
    assert restored.source is GateSource.CURRENT_DISPOSITION
    assert base.tracks(base)
    assert replace(base, reason="new diagnostics").tracks(base)
    assert restored.state is ValidatedWorkState.PARKED
    assert restored.reason == "operator approval"


def test_work_and_evidence_identity_are_separate_and_canonical():
    a, b = capture(), capture(run="another-run")
    assert a.evidence.record_id == b.evidence.record_id
    assert a.evidence.evidence_id != b.evidence.evidence_id
    assert a.evidence.record_id.startswith("r1:")
    assert a.evidence.evidence_id.startswith("e1:")
    altered = changed_observations(
        a,
        captured_at="later",
        worktree_head_sha=L,
        expected_remote_head_sha=L,
        pr_number=500,
        observed_blocking_labels=("different",),
        admitted_from_paths={ArtifactSlot.COMPLETION: "/different"},
    )
    assert a.evidence.record_id == altered.evidence.record_id
    assert a.evidence.evidence_id == altered.evidence.evidence_id
    shuffled = replace(
        a.evidence.identity,
        requested_actions=(
            RequestedAction.CREATE_PR,
            RequestedAction.PUSH_BRANCH,
            RequestedAction.CREATE_PR,
        ),
    )
    assert replace(a.evidence, identity=shuffled).evidence_id == a.evidence.evidence_id
    assert '"exchange_summary_artifact":null' in canonical_json(a.evidence.identity)
    assert (
        canonical_json(replace(a.evidence.identity.key, branch_name="caf\u00e9")).find(
            "café"
        )
        > 0
    )


def test_legacy_observations_decode_as_unobserved_remote_authority():
    evidence = capture().evidence
    observations = json.loads(canonical_json(evidence.observations))
    observations.pop("remote_baseline_status")
    decoded = decode_evidence(
        canonical_json(evidence.identity), json.dumps(observations),
    )
    assert decoded.observations.remote_baseline_status is RemoteBaselineStatus.UNOBSERVED
    assert decoded.observations.expected_remote_head_sha is None
    assert decoded.observations.pr_number is None


def test_store_migrates_legacy_queued_remote_authority_to_parked(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    admission = capture()
    rig.open().admit(admission)
    with sqlite3.connect(rig.path) as conn:
        observations = json.loads(canonical_json(admission.evidence.observations))
        observations.pop("remote_baseline_status")
        conn.execute(
            "UPDATE validated_work_evidence SET observations=? WHERE evidence_id=?",
            (canonical_json(observations), admission.evidence.evidence_id),
        )

    reopened = rig.open()
    disposition = reopened.get(admission.evidence.record_id)
    row, = reopened.retained_evidence(admission.evidence.identity.key.issue_number)
    assert disposition.state is ValidatedWorkState.PARKED
    assert disposition.failure is ValidatedWorkFailure.REMOTE_UNREADABLE
    assert row.admission.initial_state is ValidatedWorkState.PARKED
    assert row.admission.initial_failure is ValidatedWorkFailure.REMOTE_UNREADABLE
    assert row.base_state is ValidatedWorkState.PARKED
    assert row.base_failure is ValidatedWorkFailure.REMOTE_UNREADABLE
    assert row.observation_revision == 1
    assert row.admission.evidence.observations.remote_baseline_status is RemoteBaselineStatus.UNOBSERVED
    assert row.admission.evidence.observations.expected_remote_head_sha is None
    assert row.admission.evidence.observations.pr_number is None
    with sqlite3.connect(rig.path) as conn:
        expected, pr = conn.execute(
            "SELECT expected_remote_head,pr_number FROM validated_work_evidence"
        ).fetchone()
    assert expected == "" and pr is None


def test_legacy_migration_preserves_ambiguous_publishing_lifecycle(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    admission = capture()
    rig.open().admit(admission)
    with sqlite3.connect(rig.path) as conn:
        observations = json.loads(canonical_json(admission.evidence.observations))
        observations.pop("remote_baseline_status")
        conn.execute(
            "UPDATE validated_work_evidence SET observations=? WHERE evidence_id=?",
            (canonical_json(observations), admission.evidence.evidence_id),
        )
        conn.execute(
            "UPDATE validated_work_records SET state='publishing' WHERE record_id=?",
            (admission.evidence.record_id,),
        )

    reopened = rig.open()
    assert reopened.get(admission.evidence.record_id).state is ValidatedWorkState.PUBLISHING
    row, = reopened.retained_evidence(admission.evidence.identity.key.issue_number)
    assert row.admission.initial_state is ValidatedWorkState.PARKED
    assert row.base_state is ValidatedWorkState.PARKED
    assert row.base_failure is ValidatedWorkFailure.REMOTE_UNREADABLE
    assert row.admission.evidence.observations.remote_baseline_status is RemoteBaselineStatus.UNOBSERVED


def test_legacy_migration_reserves_write_authority_before_inspection(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    admission = capture(state=ValidatedWorkState.PARKED)
    rig.open().admit(admission)
    with sqlite3.connect(rig.path) as conn:
        observations = json.loads(canonical_json(admission.evidence.observations))
        observations.pop("remote_baseline_status")
        conn.execute(
            "UPDATE validated_work_evidence SET observations=? WHERE evidence_id=?",
            (canonical_json(observations), admission.evidence.evidence_id),
        )

    owner = open_sqlite(rig.path, row_factory=sqlite3.Row)
    contender = sqlite3.connect(rig.path, timeout=0)
    contender.execute("PRAGMA busy_timeout = 0")
    contender_outcomes = []

    def contend_during_inspection(statement):
        if not statement.startswith("SELECT evidence_id"):
            return
        try:
            contender.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            contender_outcomes.append(str(exc))
        else:
            contender_outcomes.append("acquired")

    owner.set_trace_callback(contend_during_inspection)
    try:
        migrate_remote_baseline_authority(owner)
    finally:
        owner.close()
        contender.close()

    assert contender_outcomes == ["database is locked"]
    reopened = rig.open()
    (row,) = reopened.retained_evidence(6914)
    assert (
        row.admission.evidence.observations.remote_baseline_status
        is RemoteBaselineStatus.UNOBSERVED
    )


def test_existing_database_migrates_separate_base_gate_from_admission(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    admission = capture(
        state=ValidatedWorkState.PARKED,
        failure=ValidatedWorkFailure.WORKSPACE_INTEGRITY,
        reason="operator approval",
    )
    rig.open().admit(admission)
    with sqlite3.connect(rig.path) as conn:
        conn.execute("ALTER TABLE validated_work_evidence DROP COLUMN base_reason")
        conn.execute("ALTER TABLE validated_work_evidence DROP COLUMN base_failure")
        conn.execute("ALTER TABLE validated_work_evidence DROP COLUMN base_state")

    row, = rig.open().retained_evidence(admission.evidence.identity.key.issue_number)

    assert row.base_state is ValidatedWorkState.PARKED
    assert row.base_failure is ValidatedWorkFailure.WORKSPACE_INTEGRITY
    assert row.base_reason == "operator approval"


def test_existing_database_migration_is_serialized_across_openers(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    admission = capture()
    rig.open().admit(admission)
    with sqlite3.connect(rig.path) as conn:
        conn.execute("ALTER TABLE validated_work_evidence DROP COLUMN base_reason")
        conn.execute("ALTER TABLE validated_work_evidence DROP COLUMN base_failure")
        conn.execute("ALTER TABLE validated_work_evidence DROP COLUMN base_state")
    barrier = Barrier(8)

    def reopen(_: int):
        barrier.wait()
        row, = Rig(rig.path).open().retained_evidence(
            admission.evidence.identity.key.issue_number
        )
        return row.base_state

    with ThreadPoolExecutor(max_workers=8) as workers:
        states = tuple(workers.map(reopen, range(8)))

    assert states == (ValidatedWorkState.QUEUED,) * 8


@pytest.mark.parametrize(
    "field,value",
    [
        ("validated_head_sha", "A" * 40),
        ("validated_head_sha", "abc"),
        ("issue_number", 0),
        ("issue_number", True),
        ("repo_slug", " "),
        ("branch_name", ""),
    ],
)
def test_key_constructor_rejects_invalid_identity(field, value):
    with pytest.raises(ValueError):
        replace(capture().evidence.identity.key, **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("branch_binding_verified", "yes"),
        ("requested_actions", ()),
        ("requested_actions", ("push_branch",)),
        ("review_disposition", "route_to_pr_review"),
        ("completion_artifact", AdmittedArtifact(ArtifactSlot.VALIDATION, "c" * 64, 1)),
        ("review_disposition", ReviewDisposition.EXCHANGE_APPROVED),
    ],
)
def test_evidence_constructor_rejects_invalid_shape(field, value):
    with pytest.raises(ValueError):
        replace(capture().evidence.identity, **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", "a" * 63),
        ("sha256", "A" * 64),
        ("byte_size", -1),
        ("byte_size", True),
        ("slot", "completion"),
    ],
)
def test_artifact_constructor_rejects_invalid_shape(field, value):
    with pytest.raises(ValueError):
        replace(capture().evidence.identity.completion_artifact, **{field: value})


def test_observation_mapping_cannot_mutate_after_construction():
    paths = {ArtifactSlot.COMPLETION: "/original"}
    obs = replace(capture().evidence.observations, admitted_from_paths=paths)
    paths[ArtifactSlot.COMPLETION] = "/moved"
    assert obs.admitted_from_paths[ArtifactSlot.COMPLETION] == "/original"
    with pytest.raises(TypeError):
        obs.admitted_from_paths[ArtifactSlot.COMPLETION] = "/bad"


def test_state_partition_and_batch_shape(tmp_path):
    assert UNRESOLVED_STATES | RESOLVED_STATES == set(ValidatedWorkState)
    assert not UNRESOLVED_STATES & RESOLVED_STATES
    assert ValidatedWorkState.FAILED in UNRESOLVED_STATES
    store = Rig(tmp_path / "work.sqlite").open()
    a = store.admit(capture()).disposition
    b = store.admit(capture(branch="second")).disposition
    batch = ValidatedWorkDispositionBatch(6914, (a, b), "both")
    assert batch.found_work and batch.unresolved
    assert batch.unresolved_dispositions == (a, b)
    assert not ValidatedWorkDispositionBatch.no_work(6914, "none").found_work
    for items, issue in [((a, a), 6914), ((a,), 12), ([a], 6914)]:
        with pytest.raises(ValueError):
            ValidatedWorkDispositionBatch(issue, items, "invalid")
    for kwargs in (
        {"record_id": "wrong"},
        {"state": ValidatedWorkState.FAILED},
        {"state": "queued"},
        {"state": ValidatedWorkState.RECOVERED},
        {"state": ValidatedWorkState.ABANDONED},
        {"resolution": OperatorResolution("operator", "reason", AT)},
    ):
        with pytest.raises(ValueError):
            replace(a, **kwargs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("record_id", "wrong"),
        ("observation_revision", -1),
        ("observation_revision", True),
        ("evidence_id", ""),
        ("expected_remote_head_sha", "bad"),
        ("pr_number", 0),
    ],
)
def test_authority_constructor_binds_exact_work(tmp_path, field, value):
    store = Rig(tmp_path / "work.sqlite").open()
    a = capture()
    store.admit(a)
    snapshot = store.evidence_for_id(a.evidence.evidence_id).evidence.authority
    with pytest.raises(ValueError):
        replace(snapshot, **{field: value})


def test_stored_recovery_cannot_be_automatic_or_redirect_authority(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    a = capture()
    store.admit(a)
    authority = store.evidence_for_id(a.evidence.evidence_id).evidence.authority
    cmd = StoredEvidenceCommand(
        6914,
        "recover",
        DispositionInitiator.OPERATOR,
        a.evidence.evidence_id,
        "user",
        authority,
    )
    for kwargs in (
        {"initiator": DispositionInitiator.AUTOMATIC},
        {"initiator": "operator"},
        {"actor": " "},
        {"issue_number": 1},
        {"evidence_id": "elsewhere"},
    ):
        with pytest.raises(ValueError):
            replace(cmd, **kwargs)


@pytest.mark.parametrize("operation", [copy, deepcopy, pickle.dumps])
def test_claim_secret_and_claim_are_private_capabilities(operation):
    secret = ClaimSecret()
    token = ValidatedWorkClaim("r1:example", 1, secret, OWNER)
    assert secret.digest() not in repr(secret)
    assert secret.digest() not in repr(token)
    with pytest.raises(TypeError):
        operation(secret)
    with pytest.raises(TypeError):
        operation(token)


def test_initial_failed_disposition_requires_failure():
    with pytest.raises(ValueError):
        capture(state=ValidatedWorkState.FAILED)
    assert capture(
        state=ValidatedWorkState.FAILED, failure=ValidatedWorkFailure.ARTIFACT_MISSING
    )
