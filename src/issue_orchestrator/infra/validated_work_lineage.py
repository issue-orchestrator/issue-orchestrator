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
    carrier_ref,
    EvidenceRow,
    LineageCarrier,
    LineageLanding,
    LineagePublication,
    PublicationProvenance,
)
from ..domain.validated_work_gate import DispositionGate
from ..domain.validated_work_remote_authority import CarriedByIssuePullRequest
from ..ports.validated_work_verification import (
    ValidatedWorkAncestry,
    ValidatedWorkArtifactVerifier,
)
from .validated_work_rows import (
    carriers, current_evidence, landings, publication, refresh_observations,
)


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
    # The open PR the containing head was observed on (OBSERVED_OPEN_PR), else 0.
    contained_pr: int = 0
    contained_kind: ResolutionKind = ResolutionKind.CONTAINED_IN_PUBLISHED_HEAD
    # The containing PR's branch when it is not the record's own (#8137), else ''.
    contained_branch: str = ""
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

    def retain(self, reference: CommitReference) -> bool:
        result = self._ancestry.retain(reference)
        if type(result) is not bool:
            raise TypeError("ancestry provider must return bool")
        return result

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
        carried: CarriedByIssuePullRequest | None = None,
    ) -> None:
        """Reclassify the lineage's unresolved records.

        A merged carrier is final and always counts. An open PR can be
        force-pushed or closed after it was proven (#8137 review r1/r2), so an
        open carrier counts only when the caller hands in that very proof as
        ``carried``: the proof's own reclassification, and the admission of
        the capture whose remote observation just made it. No stored row or
        timestamp alone authorizes it - a replayed or later admission is
        resolved only by a proof of its own.
        """
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
        readable = self._classify_publication(
            conn, decisions, fact, landings(conn, lineage_key), tuple(
                carrier for carrier in carriers(conn, lineage_key)
                if carrier.merged or (
                    carried is not None and not carried.merged
                    and (carrier.pr_number, carrier.head_sha) == (carried.pr_number, carried.head_sha)
                )
            ),
        )
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
        base = DispositionGate.from_evidence(evidence)
        if row["record_id"] in reconsider:
            gate = base
        else:
            gate = DispositionGate(
                State(row["state"]),
                Failure(row["failure"]) if row["failure"] else None,
                row["reason"],
            ).restore(base)
        return LineageDecision(evidence, gate.state, gate.failure, gate.reason)

    def _classify_publication(
        self,
        conn: sqlite3.Connection,
        decisions: list[LineageDecision],
        fact: LineagePublication | None,
        landed: tuple[LineageLanding, ...],
        carried: tuple[LineageCarrier, ...],
    ) -> list[LineageDecision]:
        readable: list[LineageDecision] = []
        for decision in decisions:
            if (
                self.compare(decision.reference, decision.reference)
                is not Relation.EQUAL
            ):
                self._unreachable(decision)
                continue
            if self._within_landing(decision, landed) or self._within_carrier(decision, carried):
                continue
            if fact is not None and self._against_publication(conn, decision, fact):
                continue
            readable.append(decision)
        return readable

    def _within_landing(
        self, decision: LineageDecision, landed: tuple[LineageLanding, ...]
    ) -> bool:
        """Resolve a record a merged PR's head at merge contains (§2.8).

        Checked before the lineage fact: shipping is final, whatever the
        branch publishes now. Only containment counts - a head ahead of or
        beside every landing is left to the fact's rules.
        """
        for landing in landed:
            # Through its pin: a landing whose pin is gone proves nothing.
            head = CommitReference(
                replace(decision.reference.key, validated_head_sha=landing.head_sha), landing.ref
            )
            if self.compare(decision.reference, head) not in {Relation.EQUAL, Relation.ANCESTOR}:
                continue
            self._contained(decision, landing.head_sha, landing.pr_number,
                            ResolutionKind.LANDED_VIA_MERGED_PR)
            return True
        return False

    def _within_carrier(
        self, decision: LineageDecision, carried: tuple[LineageCarrier, ...]
    ) -> bool:
        """Resolve a record another branch's PR of its issue carries (#8137).

        Checked before the lineage fact, like a landing: the work is published
        under that PR whatever this branch publishes now. Only containment
        counts, and only through the carrier's pin.
        """
        for carrier in carried:
            head = CommitReference(
                replace(decision.reference.key, validated_head_sha=carrier.head_sha),
                carrier_ref(carrier.lineage_key, carrier.pr_number, carrier.head_sha),
            )
            if self.compare(decision.reference, head) not in {Relation.EQUAL, Relation.ANCESTOR}:
                continue
            kind = (
                ResolutionKind.LANDED_VIA_MERGED_PR if carrier.merged
                else ResolutionKind.CONTAINED_IN_PUBLISHED_HEAD
            )
            self._contained(decision, carrier.head_sha, carrier.pr_number, kind,
                            branch=carrier.branch_name)
            return True
        return False

    def _contained(
        self, decision: LineageDecision, head_sha: str, pr_number: int, kind: ResolutionKind,
        *, branch: str = "",
    ) -> None:
        if self.verifies(decision.evidence):
            decision.state, decision.failure = State.RECOVERED, None
            decision.reason, decision.contained_at = kind.value, head_sha
            decision.contained_pr, decision.contained_kind = pr_number, kind
            decision.contained_branch = branch
        else:
            decision.state, decision.failure, decision.reason = (
                State.FAILED,
                Failure.ARTIFACT_HASH_MISMATCH,
                Failure.ARTIFACT_HASH_MISMATCH.value,
            )

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
            self._contained(decision, fact.published_head_sha, fact.published_pr_number,
                            ResolutionKind.CONTAINED_IN_PUBLISHED_HEAD)
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
                "UPDATE validated_work_records SET published_head_sha=?, resolution_kind=?, resolved_at=?, terminal_at=?, "
                "finalization_phase='complete', published_pr_number=?, published_branch=? WHERE record_id=?",
                (
                    decision.contained_at,
                    decision.contained_kind.value,
                    at,
                    at,
                    decision.contained_pr,
                    decision.contained_branch,
                    decision.record_id,
                ),
            )
