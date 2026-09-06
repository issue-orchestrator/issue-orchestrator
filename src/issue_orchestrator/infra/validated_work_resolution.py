"""Atomic verified resolution plus publication fact, ancestors and waiters."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from ..domain.validated_work import (
    ResolutionKind,
    ValidatedWorkFailure as Failure,
    ValidatedWorkState as State,
    require_sha,
)
from ..domain.validated_work_claim import ValidatedWorkClaim
from ..domain.validated_work_store import (
    AncestryRelation as Relation,
    CommitReference,
    FinalizationPhase,
    LineageResolutionRefusal as Refusal,
    PublicationProvenance as Provenance,
    PublicationResolution,
)
from .validated_work_claims import ClaimAuthority, owner_identity
from .validated_work_lineage import LineageClassifier
from .validated_work_rows import current_evidence, disposition, publication, record_row


class PublicationResolver:
    def __init__(self, claims: ClaimAuthority, lineage: LineageClassifier) -> None:
        self._claims = claims
        self._lineage = lineage

    def published(
        self,
        conn: sqlite3.Connection,
        claim: ValidatedWorkClaim,
        *,
        record_id: str,
        published_head_sha: str,
        pre_push_expected: str,
        finalized_at: str,
    ) -> PublicationResolution | Refusal:
        if claim.record_id != record_id or not self._claims.holds(conn, claim):
            return Refusal.STALE_CLAIM
        row = record_row(conn, record_id)
        if (
            row["state"] != "publishing"
            or row["finalization_phase"] != FinalizationPhase.RECOVERY_CLEARED
        ):
            return Refusal.FINALIZATION_INCOMPLETE
        evidence = current_evidence(conn, record_id)
        if (
            published_head_sha != evidence.authority.validated_head_sha
            or pre_push_expected != (evidence.authority.expected_remote_head_sha or "")
        ):
            return Refusal.CONTAINMENT_UNPROVEN
        return self._resolve(
            conn,
            record_id,
            published_head_sha,
            pre_push_expected,
            finalized_at,
            Provenance.PUSHED_BY_OWNER,
        )

    def observed_merge(
        self,
        conn: sqlite3.Connection,
        *,
        record_id: str,
        merged_head_sha: str,
        observed_at: str,
    ) -> PublicationResolution | Refusal:
        row = record_row(conn, record_id)
        if row["state"] == "publishing" or owner_identity(row) is not None:
            return Refusal.PUBLICATION_IN_FLIGHT
        return self._resolve(
            conn, record_id, merged_head_sha, "", observed_at, Provenance.OBSERVED_MERGE
        )

    def _resolve(
        self,
        conn: sqlite3.Connection,
        record_id: str,
        head: str,
        expected: str,
        at: str,
        via: Provenance,
    ) -> PublicationResolution | Refusal:
        require_sha(head)
        row, evidence = record_row(conn, record_id), current_evidence(conn, record_id)
        key = evidence.admission.evidence.identity.key
        target = CommitReference(replace(key, validated_head_sha=head), "")
        if self._lineage.compare(
            CommitReference(key, evidence.admission.pinned_ref), target
        ) not in {Relation.EQUAL, Relation.ANCESTOR}:
            return Refusal.CONTAINMENT_UNPROVEN
        if not self._lineage.verifies(evidence):
            return Refusal.CONTAINMENT_UNPROVEN
        old_fact = publication(conn, row["lineage_key"])
        if old_fact is not None:
            old_target = CommitReference(
                replace(key, validated_head_sha=old_fact.published_head_sha), ""
            )
            if self._lineage.compare(old_target, target) not in {
                Relation.EQUAL,
                Relation.ANCESTOR,
            }:
                return Refusal.NOT_A_DESCENDANT
        peers = conn.execute(
            "SELECT record_id,state,waits_on_record_id FROM validated_work_records WHERE lineage_key=? AND record_id!=? AND state IN ('queued','parked','failed','publishing')",
            (row["lineage_key"], record_id),
        ).fetchall()
        if any(peer["state"] == "publishing" for peer in peers):
            return Refusal.PUBLICATION_IN_FLIGHT
        if row["state"] != "recovered" or row["published_head_sha"] != head:
            self._record_resolution(conn, record_id, head, via, at)
        # Equal observations must not erase an already proven pre-push baseline.
        if old_fact is None or old_fact.published_head_sha != head:
            conn.execute(
                "INSERT INTO validated_work_lineage VALUES (?,?,?,?,?,?) ON CONFLICT(lineage_key) DO UPDATE SET "
                "published_head_sha=excluded.published_head_sha,published_by_record_id=excluded.published_by_record_id,"
                "published_via=excluded.published_via,published_pre_push_expected=excluded.published_pre_push_expected,published_at=excluded.published_at",
                (row["lineage_key"], head, record_id, via.value, expected, at),
            )
        self._lineage.classify(conn, row["lineage_key"], at)
        fact = publication(conn, row["lineage_key"])
        assert fact is not None
        after = {
            peer["record_id"]: disposition(conn, peer["record_id"]) for peer in peers
        }
        return PublicationResolution(
            disposition(conn, record_id),
            fact,
            tuple(d for d in after.values() if d.state is State.RECOVERED),
            tuple(
                d
                for d in after.values()
                if d.state is State.FAILED
                and d.failure is Failure.ARTIFACT_HASH_MISMATCH
                and self._lineage.compare(
                    CommitReference(
                        d.key, current_evidence(conn, d.record_id).admission.pinned_ref
                    ),
                    target,
                )
                in {Relation.EQUAL, Relation.ANCESTOR}
            ),
            tuple(
                after[p["record_id"]]
                for p in peers
                if p["waits_on_record_id"] == record_id
            ),
        )

    @staticmethod
    def _record_resolution(
        conn: sqlite3.Connection, record_id: str, head: str, via: Provenance, at: str
    ) -> None:
        kind = (
            ResolutionKind.PUBLISHED
            if via is Provenance.PUSHED_BY_OWNER
            else ResolutionKind.CONTAINED_IN_PUBLISHED_HEAD
        )
        conn.execute(
            "UPDATE validated_work_records SET state='recovered',failure='',reason=?,published_head_sha=?,resolution_kind=?,"
            "finalization_phase='complete',resolved_by='',resolution_reason='',resolved_at=?,terminal_at=?,updated_at=?,superseded_by_record_id='',waits_on_record_id='' WHERE record_id=?",
            (kind.value, head, kind.value, at, at, at, record_id),
        )
