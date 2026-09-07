"""Public port compatibility and remaining fail-closed boundary cases."""

from dataclasses import replace
import inspect
import sqlite3
from contextlib import closing

import pytest

from issue_orchestrator.domain.validated_work import (
    ValidatedWorkFailure as Failure,
    ValidatedWorkState as State,
)
from issue_orchestrator.domain.validated_work_store import (
    DispositionPhase,
    LineagePublication,
    PublicationProvenance,
    PublishAttempt,
    PublishValidatedHeadStatus as Status,
)
from issue_orchestrator.infra.validated_work_store import SqliteValidatedWorkStore
from issue_orchestrator.ports.validated_work_store import (
    ValidatedWorkFence,
    ValidatedWorkStore,
)
from tests.unit.validated_work_support import (
    AT,
    LATER,
    OWNER,
    OTHER,
    ROOT,
    V,
    DIVERGENT,
    Rig,
    Liveness,
    begin,
    capture,
    claim,
)


def as_store_port(store: SqliteValidatedWorkStore) -> ValidatedWorkStore:
    """Also checked statically: the concrete surface must implement the whole port."""
    return store


def as_fence_port(store: SqliteValidatedWorkStore) -> ValidatedWorkFence:
    return store


@pytest.mark.parametrize("state", ["publising", "recovered", "abandoned"])
def test_unresolved_probe_rejects_invalid_dispositions_instead_of_no_work(
    tmp_path, state
):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    admission = capture()
    store.admit(admission)
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        conn.execute("UPDATE validated_work_records SET state=?", (state,))
    for current in (store, rig.open()):
        with pytest.raises(ValueError):
            current.get(admission.evidence.record_id)
        with pytest.raises(ValueError):
            current.has_unresolved_work(6914)
        assert not current.has_unresolved_work(6915)


def test_unresolved_probe_accepts_empty_and_legitimately_resolved_work(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    assert not store.has_unresolved_work(6914)
    admission = capture()
    store.admit(admission)
    assert store.has_unresolved_work(6914)
    store.resolve_observed_merge(
        record_id=admission.evidence.record_id, merged_head_sha=V, observed_at=LATER
    )
    assert store.get(admission.evidence.record_id).state is State.RECOVERED
    assert not store.has_unresolved_work(6914)


def test_unresolved_probe_accepts_valid_abandonment_audit(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    admission = capture()
    store.admit(admission)
    # The abandonment writer belongs to a later slice; represent its durable
    # contract here rather than treating a bare terminal state as sufficient.
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        conn.execute(
            "UPDATE validated_work_records SET state='abandoned', resolution_kind='operator_abandoned', "
            "resolved_by='operator', resolution_reason='accepted loss', resolved_at=?, terminal_at=?",
            (LATER, LATER),
        )
    reopened = rig.open()
    assert reopened.get(admission.evidence.record_id).state is State.ABANDONED
    assert not reopened.has_unresolved_work(6914)


def test_typed_ports_have_no_caller_death_timer_or_transaction_parameter(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    port = as_store_port(store)
    a = capture()
    port.admit(a)
    token = claim(store, a)
    assert as_fence_port(store).holds_claim(token)
    assert port.record_for_id(token.record_id).owner == OWNER
    for name, method in inspect.getmembers(ValidatedWorkStore, inspect.isfunction):
        if name.startswith("_"):
            continue
        parameters = inspect.signature(method).parameters
        assert (
            not {
                "liveness",
                "now",
                "lease",
                "transaction",
                "conn",
                "quiescent",
                "owner_dead",
                "verified",
            }
            & parameters.keys()
        )


@pytest.mark.parametrize("missing", ["ancestry", "artifacts", "liveness", "retention"])
def test_constructor_requires_every_verification_capability_before_creating_db(
    tmp_path, missing
):
    rig = Rig(tmp_path / "work.sqlite")
    args = {"ancestry": rig.graph, "artifacts": rig.artifacts, "retention": rig.artifacts, "liveness": rig.liveness}
    del args[missing]
    with pytest.raises(TypeError):
        SqliteValidatedWorkStore(rig.path, **args)
    args[missing] = None
    with pytest.raises(ValueError):
        SqliteValidatedWorkStore(rig.path, **args)
    assert not rig.path.exists()


def test_boolean_ancestry_fact_is_not_a_typed_proof(tmp_path):
    class BadAncestry:
        def compare(self, left, right):
            return True

    rig = Rig(tmp_path / "work.sqlite")
    store = SqliteValidatedWorkStore(
        rig.path, ancestry=BadAncestry(), artifacts=rig.artifacts, retention=rig.artifacts, liveness=rig.liveness
    )
    with pytest.raises(TypeError, match="typed relation"):
        store.admit(capture())
    assert not store.for_issue(6914).found_work


def test_merged_publication_cannot_invent_a_pre_push_baseline():
    with pytest.raises(ValueError, match="proves no"):
        LineagePublication(
            "lineage", V, "record", PublicationProvenance.OBSERVED_MERGE, ROOT, AT
        )
    with pytest.raises(ValueError, match="provenance"):
        LineagePublication("lineage", V, "record", "observed_merge", "", AT)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fence": 0},
        {"attempt_no": 0},
        {"finished_at": AT},
        {"failure": Failure.PUSH_FAILED},
        {"outcome": Status.SUPERSEDED, "finished_at": AT},
        {"outcome": Status.TRANSIENT_FAILURE, "finished_at": AT},
        {"outcome": Status.PUBLISHED},
        {
            "outcome": Status.PUBLISHED,
            "failure": Failure.PUSH_FAILED,
            "finished_at": AT,
        },
    ],
)
def test_attempt_constructor_has_total_completion_shape(kwargs):
    attempt = PublishAttempt(
        "record", 1, "evidence", V, ROOT, DispositionPhase.PRE_SUBMISSION, 1, AT
    )
    with pytest.raises(ValueError):
        replace(attempt, **kwargs)


def test_partial_unique_indexes_exclude_rival_current_and_drainable_rows(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a, newer = capture(), capture(run="newer")
    store.admit(a)
    store.admit(newer)
    store.admit(capture(DIVERGENT))
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE validated_work_evidence SET role='current' WHERE evidence_id=?",
                (a.evidence.evidence_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE validated_work_records SET state='queued'")
    assert all(row.state is State.PARKED for row in store.for_issue(6914).dispositions)


def test_outcomeless_attempts_count_against_durable_budget(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a = capture()
    store.admit(a)
    previous = OWNER
    token = claim(store, a)
    for n in range(1, 6):
        attempt = begin(store, token)
        assert attempt.attempt_no == n
        identity = replace(OTHER, pid=200 + n, started_at=LATER)
        store = rig.open(Liveness(identity, {previous}))
        token = claim(store, a)
        previous = identity
    assert begin(store, token) is None
    assert store.get(token.record_id).state is State.FAILED
    assert all(
        attempt.outcome is None for attempt in store.publish_attempts(token.record_id)
    )


def test_retention_query_addresses_every_role_but_no_unresolved_work(tmp_path):
    from tests.unit.validated_work_support import finalize

    store = Rig(tmp_path / "work.sqlite").open()
    a = capture()
    store.admit(a)
    token = claim(store, a)
    attempt = begin(store, token)
    attached = capture(run="attached")
    store.admit(attached)
    assert store.evidence_for_retention(released_before="9999") == ()
    finalize(store, token, attempt)
    assert {
        row.evidence_id for row in store.evidence_for_retention(released_before="9999")
    } == {a.evidence.evidence_id, attached.evidence.evidence_id}


def test_denormalized_observation_corruption_fails_closed(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a = capture()
    store.admit(a)
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        conn.execute(
            "UPDATE validated_work_evidence SET expected_remote_head=?", (DIVERGENT,)
        )
    with pytest.raises(ValueError, match="disagree"):
        store.evidence_for_id(a.evidence.evidence_id)


def test_truthy_artifact_fact_cannot_begin_publication(tmp_path):
    class UnverifiedArtifacts:
        def verifies(self, evidence):
            return "verified"

    rig = Rig(tmp_path / "work.sqlite")
    store = SqliteValidatedWorkStore(
        rig.path,
        ancestry=rig.graph,
        artifacts=UnverifiedArtifacts(), retention=rig.artifacts,
        liveness=rig.liveness,
    )
    a = capture()
    store.admit(a)
    token = claim(store, a)
    with pytest.raises(TypeError, match="verifier must return bool"):
        begin(store, token)
    assert store.get(token.record_id).state is State.QUEUED
    assert store.publish_attempts(token.record_id) == ()


@pytest.mark.parametrize("state", ["recovered", "abandoned"])
def test_retention_rejects_malformed_resolution_before_exposing_candidates(tmp_path, state):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    admission = capture()
    store.admit(admission)
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        conn.execute(
            "UPDATE validated_work_records SET state=?, terminal_at=?", (state, AT)
        )
    for current in (store, rig.open()):
        with pytest.raises(ValueError):
            current.evidence_for_retention(released_before="9999")
        with closing(sqlite3.connect(rig.path)) as conn:
            assert conn.execute(
                "SELECT released_at FROM validated_work_evidence"
            ).fetchall() == [("",)]
