"""Constructor and identity invariants of bounded disposition slices 1 + 1a."""

from copy import copy, deepcopy
from dataclasses import replace
import pickle

import pytest

from issue_orchestrator.domain.models import RequestedAction
from issue_orchestrator.domain.validated_work import (
    AdmittedArtifact,
    ArtifactSlot,
    RESOLVED_STATES,
    UNRESOLVED_STATES,
    ReviewDisposition,
    ValidatedWorkFailure,
    ValidatedWorkState,
    canonical_json,
)
from issue_orchestrator.domain.validated_work_claim import (
    ClaimSecret,
    ValidatedWorkClaim,
)
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
