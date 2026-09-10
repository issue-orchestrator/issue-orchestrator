"""One transaction-local owner for admission gates, lineage and containment."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace

from ..domain.validated_work import (
    LineageRole,
    RemoteBaselineStatus,
    ResolutionKind,
    ValidatedWorkFailure as Failure,
    ValidatedWorkState as State,
)
from ..domain.validated_work_store import (
    AncestryRelation as Relation,
    CommitReference,
    EvidenceRow,
    LineagePublication,
    PublicationProvenance,
)
from ..domain.validated_work_gate import DispositionGate
from ..ports.validated_work_verification import (
    ValidatedWorkAncestry,
    ValidatedWorkArtifactVerifier,
)
from .validated_work_rows import current_evidence, publication, refresh_observations


@dataclass
class LineageDecision:
    evidence: EvidenceRow
    state: State
    failure: Failure | None
    reason: str
    role: LineageRole = LineageRole.HEAD
    superseded_by: str = ""
    waits_on: str = ""
    contained_at: str = ""
    reachable: bool = True

    @property
    def record_id(self) -> str:
        return self.evidence.record_id

    @property
    def reference(self) -> CommitReference:
        admission = self.evidence.admission
        return CommitReference(admission.evidence.identity.key, admission.pinned_ref)

    def restrict(self, role: LineageRole, failure: Failure) -> None:
        self.role = role
        # Evidence validation failures are never softened to parked by lineage.
        if self.state is not State.FAILED:
            self.state, self.failure, self.reason = State.PARKED, failure, failure.value


class LineageClassifier:
    """Compose evidence eligibility with commit relationships in one place."""

    def __init__(
        self, ancestry: ValidatedWorkAncestry, verifier: ValidatedWorkArtifactVerifier
    ) -> None:
        self._ancestry = ancestry
        self._verifier = verifier

    def compare(self, left: CommitReference, right: CommitReference) -> Relation:
        relation = self._ancestry.compare(left, right)
        if not isinstance(relation, Relation):
            raise TypeError("ancestry provider must return a typed relation")
        return relation

    def verifies(self, evidence: EvidenceRow) -> bool:
        result = self._verifier.verifies(evidence)
        if type(result) is not bool:
            raise TypeError("artifact verifier must return bool")
        return result

    def classify(
        self,
        conn: sqlite3.Connection,
        lineage_key: str,
        at: str,
        *,
        reconsider: frozenset[str] = frozenset(),
    ) -> None:
        rows = conn.execute(
            "SELECT * FROM validated_work_records WHERE lineage_key=? "
            "AND state IN ('queued','parked','failed','publishing') ORDER BY created_at, record_id",
            (lineage_key,),
        ).fetchall()
        publishing = next((r for r in rows if r["state"] == "publishing"), None)
        if publishing is not None:
            self._defer_arrivals(conn, rows, publishing["record_id"], reconsider, at)
            return
        fact = publication(conn, lineage_key)
        decisions = [self._restore_gate(conn, row, reconsider) for row in rows]
        readable = self._classify_publication(conn, decisions, fact)
        self._classify_peers(readable)
        # Vacate the unique drainable slot before installing a descendant. The
        # intermediate state is private to this IMMEDIATE transaction.
        conn.execute(
            "UPDATE validated_work_records SET state='parked' WHERE lineage_key=? AND state='queued'",
            (lineage_key,),
        )
        for decision in decisions:
            original = next(
                row for row in rows if row["record_id"] == decision.record_id
            )
            self._persist(conn, decision, at, original)

    @staticmethod
    def select_for_publication(
        conn: sqlite3.Connection, record_id: str, at: str
    ) -> bool:
        """Install one snapshot-approved choice without racing a publishing peer."""
        row = conn.execute(
            "SELECT lineage_key FROM validated_work_records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        if conn.execute(
            "SELECT 1 FROM validated_work_records WHERE lineage_key=? AND state='publishing' AND record_id!=?",
            (row["lineage_key"], record_id),
        ).fetchone():
            return False
        conn.execute(
            "UPDATE validated_work_records SET state='parked',lineage_role='pending',waits_on_record_id=?,superseded_by_record_id='',"
            "failure=?,reason=?,updated_at=? WHERE lineage_key=? AND state='queued' AND record_id!=?",
            (
                record_id,
                Failure.AWAITING_LINEAGE_PREDECESSOR.value,
                Failure.AWAITING_LINEAGE_PREDECESSOR.value,
                at,
                row["lineage_key"],
                record_id,
            ),
        )
        conn.execute(
            "UPDATE validated_work_records SET lineage_role='head',superseded_by_record_id='',waits_on_record_id='' WHERE record_id=?",
            (record_id,),
        )
        return True

    def _defer_arrivals(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
        predecessor: str,
        reconsider: frozenset[str],
        at: str,
    ) -> None:
        for row in rows:
            if row["record_id"] == predecessor or row["record_id"] not in reconsider:
                continue
            conn.execute(
                "UPDATE validated_work_records SET state='parked', lineage_role='pending', failure=?, reason=?, "
                "waits_on_record_id=?, superseded_by_record_id='', updated_at=? WHERE record_id=?",
                (
                    Failure.AWAITING_LINEAGE_PREDECESSOR.value,
                    Failure.AWAITING_LINEAGE_PREDECESSOR.value,
                    predecessor,
                    at,
                    row["record_id"],
                ),
            )

    @staticmethod
    def _restore_gate(
        conn: sqlite3.Connection, row: sqlite3.Row, reconsider: frozenset[str]
    ) -> LineageDecision:
        evidence = current_evidence(conn, row["record_id"])
        if row["record_id"] in reconsider:
            gate = DispositionGate.admitted(evidence.admission)
        else:
            gate = DispositionGate(
                State(row["state"]),
                Failure(row["failure"]) if row["failure"] else None,
                row["reason"],
            ).restore(evidence.admission)
        return LineageDecision(evidence, gate.state, gate.failure, gate.reason)

    def _classify_publication(
        self,
        conn: sqlite3.Connection,
        decisions: list[LineageDecision],
        fact: LineagePublication | None,
    ) -> list[LineageDecision]:
        readable: list[LineageDecision] = []
        for decision in decisions:
            if (
                self.compare(decision.reference, decision.reference)
                is not Relation.EQUAL
            ):
                self._unreachable(decision)
                continue
            if fact is not None and self._against_publication(conn, decision, fact):
                continue
            readable.append(decision)
        return readable

    def _against_publication(
        self,
        conn: sqlite3.Connection,
        decision: LineageDecision,
        fact: LineagePublication,
    ) -> bool:
        key = replace(
            decision.reference.key, validated_head_sha=fact.published_head_sha
        )
        reference = CommitReference(
            key, ""
        )  # durable published object; not a mutable branch
        relation = self.compare(decision.reference, reference)
        if relation in {Relation.EQUAL, Relation.ANCESTOR}:
            if self.verifies(decision.evidence):
                decision.state, decision.failure = State.RECOVERED, None
                decision.reason, decision.contained_at = (
                    ResolutionKind.CONTAINED_IN_PUBLISHED_HEAD.value,
                    fact.published_head_sha,
                )
            else:
                decision.state, decision.failure, decision.reason = (
                    State.FAILED,
                    Failure.ARTIFACT_HASH_MISMATCH,
                    Failure.ARTIFACT_HASH_MISMATCH.value,
                )
            return True
        if relation is Relation.DESCENDANT:
            self._sequence_baseline(conn, decision, fact)
            return False
        if relation is Relation.DIVERGENT:
            decision.restrict(LineageRole.DIVERGENT, Failure.DIVERGENT_VALIDATED_HEADS)
            return False
        self._unreachable(decision)
        return True

    @staticmethod
    def _sequence_baseline(
        conn: sqlite3.Connection, decision: LineageDecision, fact: LineagePublication
    ) -> None:
        obs = decision.evidence.admission.evidence.observations
        expected = obs.expected_remote_head_sha or ""
        proven = obs.remote_baseline_status is RemoteBaselineStatus.OBSERVED and (
            expected == fact.published_head_sha or (
            fact.published_via is PublicationProvenance.PUSHED_BY_OWNER
            and expected == fact.published_pre_push_expected
            )
        )
        if not proven:
            decision.restrict(LineageRole.HEAD, Failure.REMOTE_BASELINE_UNPROVEN)
        elif expected != fact.published_head_sha:
            refresh_observations(
                conn,
                decision.evidence.evidence_id,
                replace(obs, expected_remote_head_sha=fact.published_head_sha),
            )

    def _classify_peers(self, decisions: list[LineageDecision]) -> None:
        comparisons = self._compare_peers(decisions)
        readable = [d for d in decisions if d.reachable]
        divergent = any(d.role is LineageRole.DIVERGENT for d in readable) or any(
            relation is Relation.DIVERGENT for _, _, relation in comparisons
        )
        if divergent:
            for decision in readable:
                decision.restrict(
                    LineageRole.DIVERGENT, Failure.DIVERGENT_VALIDATED_HEADS
                )
            return
        ancestors = [
            (a, b) if relation is Relation.ANCESTOR else (b, a)
            for a, b, relation in comparisons
        ]
        self._assign_ancestors(readable, ancestors)

    def _compare_peers(
        self, decisions: list[LineageDecision]
    ) -> list[tuple[LineageDecision, LineageDecision, Relation]]:
        """Identify unreadable objects before applying any relationship to peers."""
        comparisons: list[tuple[LineageDecision, LineageDecision, Relation]] = []
        for index, left in enumerate(decisions):
            for right in decisions[index + 1 :]:
                relation = self.compare(left.reference, right.reference)
                if relation in {
                    Relation.ANCESTOR,
                    Relation.DESCENDANT,
                    Relation.DIVERGENT,
                }:
                    comparisons.append((left, right, relation))
                else:
                    self._mark_unreadable_pair(left, right, relation)
        return [
            (a, b, relation)
            for a, b, relation in comparisons
            if a.reachable and b.reachable
        ]

    @staticmethod
    def _assign_ancestors(
        decisions: list[LineageDecision],
        relations: list[tuple[LineageDecision, LineageDecision]],
    ) -> None:
        # Choose the ultimate descendant, independent of admission ordering.
        for ancestor, descendant in relations:
            ancestor.restrict(LineageRole.ANCESTOR, Failure.ANCESTOR_OF_PENDING_HEAD)
            if not ancestor.superseded_by:
                ancestor.superseded_by = descendant.record_id
        by_id = {d.record_id: d for d in decisions}
        for decision in decisions:
            while (
                decision.superseded_by and by_id[decision.superseded_by].superseded_by
            ):
                decision.superseded_by = by_id[decision.superseded_by].superseded_by

    @staticmethod
    def _mark_unreadable_pair(
        left: LineageDecision, right: LineageDecision, relation: Relation
    ) -> None:
        if relation in {Relation.LEFT_UNREACHABLE, Relation.BOTH_UNREACHABLE}:
            LineageClassifier._unreachable(left)
        if relation in {Relation.RIGHT_UNREACHABLE, Relation.BOTH_UNREACHABLE}:
            LineageClassifier._unreachable(right)
        if relation is Relation.EQUAL:
            raise ValueError(
                "distinct work records in a lineage cannot have equal heads"
            )

    @staticmethod
    def _unreachable(decision: LineageDecision) -> None:
        decision.reachable = False
        decision.state, decision.failure, decision.reason = (
            State.FAILED,
            Failure.VALIDATION_SHA_MISMATCH,
            Failure.VALIDATION_SHA_MISMATCH.value,
        )

    @staticmethod
    def _persist(
        conn: sqlite3.Connection,
        decision: LineageDecision,
        at: str,
        original: sqlite3.Row,
    ) -> None:
        persisted = (
            decision.state.value,
            decision.failure.value if decision.failure else "",
            decision.reason,
            decision.role.value,
            decision.superseded_by,
            decision.waits_on,
        )
        previous = tuple(
            original[key]
            for key in (
                "state",
                "failure",
                "reason",
                "lineage_role",
                "superseded_by_record_id",
                "waits_on_record_id",
            )
        )
        updated_at = at if persisted != previous else original["updated_at"]
        conn.execute(
            "UPDATE validated_work_records SET state=?, failure=?, reason=?, lineage_role=?, "
            "superseded_by_record_id=?, waits_on_record_id=?, updated_at=? WHERE record_id=?",
            (
                decision.state.value,
                decision.failure.value if decision.failure else "",
                decision.reason,
                decision.role.value,
                decision.superseded_by,
                decision.waits_on,
                updated_at,
                decision.record_id,
            ),
        )
        if decision.contained_at:
            conn.execute(
                "UPDATE validated_work_records SET published_head_sha=?, resolution_kind=?, resolved_at=?, terminal_at=?, finalization_phase='complete' WHERE record_id=?",
                (
                    decision.contained_at,
                    ResolutionKind.CONTAINED_IN_PUBLISHED_HEAD.value,
                    at,
                    at,
                    decision.record_id,
                ),
            )
