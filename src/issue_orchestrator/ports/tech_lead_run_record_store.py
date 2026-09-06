"""Port for the LOCAL tech-lead run-record store (ADR-0033 / #6858).

The sibling of :mod:`.run_ledger_store`, and its opposite in every dimension
that matters. The ledger is SHARED (GitHub-backed), holds only what a peer
engine must know — "claimed by X until T" — and is consulted to make decisions.
This store is LOCAL, holds everything an *operator* wants ("what did the last
health review conclude, and where is its transcript?"), and is never consulted
to decide anything. Keeping them as two ports is what stops the run record
drifting back onto the client's board: nothing here can be read by a peer, so
nothing here can be load-bearing for coordination.

Exception contract, mirroring the ledger's: implementations MUST NOT raise for
an unreachable backing store. Visibility bookkeeping failing is never a reason
to fail a tech-lead run — the run is the product, the record is the receipt —
so writes are best-effort and display reads degrade to "no history", loudly logged
by the implementation rather than silently swallowed by callers. Delivery
inspection instead returns explicit unknown evidence on errors.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Optional, Protocol, Sequence

from ..domain.tech_lead_delivery import (
    DeliveryHistoryState,
    TechLeadDeliveryEvidence,
    delivery_time,
)
from ..domain.tech_lead_run_artifacts import TechLeadRunArtifacts
from ..domain.tech_lead_run_record import (
    TechLeadDeliveryOutcome, TechLeadRunPhase, TechLeadRunRecord,
)


class TechLeadDeliveryHistoryReader(Protocol):
    """Aggregate inspection evidence, never a truncated display history.

    Unavailable or incomplete history must be explicit; it is not evidence of
    healthy delivery. Withdrawn runs do not contribute to launch pressure.
    """

    def inspect_delivery_evidence(self) -> TechLeadDeliveryEvidence:
        """Read all receipt evidence, explicitly reporting errors or lost history."""
        ...


class TechLeadRunHistoryReader(TechLeadDeliveryHistoryReader, Protocol):
    """The read half, for surfaces that display history and never write it.

    Narrower than the store on purpose: the dashboard projection depends on
    THIS, so a view can never reach a write path, and "there is no engine to
    read" has one representation (:data:`NO_TECH_LEAD_RUN_HISTORY`) instead of
    an ``Optional`` every caller re-checks.
    """

    def recent(self, *, limit: int) -> tuple[TechLeadRunRecord, ...]:
        """The newest ``limit`` runs, most recently STARTED first."""
        ...


class _EmptyTechLeadRunHistory:
    """The history of an engine that is not there."""

    def inspect_delivery_evidence(self) -> TechLeadDeliveryEvidence:
        return TechLeadDeliveryEvidence()

    def recent(self, *, limit: int) -> tuple[TechLeadRunRecord, ...]:
        return ()


NO_TECH_LEAD_RUN_HISTORY: TechLeadRunHistoryReader = _EmptyTechLeadRunHistory()


class TechLeadRunRecordStore(TechLeadDeliveryHistoryReader, Protocol):
    """Durable local history of this engine's tech-lead runs."""

    def open_run(self, record: TechLeadRunRecord) -> None:
        """Record that ``record``'s run has started.

        Idempotent on the session run identity: re-opening the same run
        replaces the row rather than appending a second history entry, so a
        retried launch of one run reads as one run.
        """
        ...

    def conclude_run(
        self,
        *,
        run_id: str,
        session_name: str,
        phase: TechLeadRunPhase,
        ended_at: datetime,
        detail: str = "",
        findings: int = 0,
        proposals: int = 0,
        artifacts: Optional[TechLeadRunArtifacts] = None,
        delivery_outcome: TechLeadDeliveryOutcome = TechLeadDeliveryOutcome.LEGACY,
    ) -> None:
        """Close the open record for one session run.

        ``delivery_outcome`` is the terminal owner's post-apply proof, persisted
        atomically with the conclusion. LEGACY is for historical callers only;
        an ambiguous old human phase supplies unknown delivery evidence.

        A no-op when no record was opened — a run this engine never recorded
        (an older engine's, or one whose open write failed) must not resurrect
        as a phantom conclusion with no start.

        ``artifacts`` is the locator for what the run left behind, recorded at
        the SAME write as the verdict: the two are one fact ("it ended, and this
        is the evidence"), and splitting them would let a crash between the two
        leave a concluded run advertising a drill-down that was never filed.
        """
        ...

    def forget_artifacts(self, locations: "Sequence[Path]") -> None:
        """Drop the artifact locator from every row pointing at ``locations``.

        The other half of the archive's retention pass (#6858 round 2 F6): the
        bytes and the row that advertises them are retired together, so a
        recorded run never offers a drill-down into a directory that is gone. The
        VERDICT is untouched — a run whose evidence aged out still happened, and
        still concluded what it concluded.
        """
        ...

    def recent(self, *, limit: int) -> tuple[TechLeadRunRecord, ...]:
        """The newest ``limit`` runs, most recently STARTED first."""
        ...


class InMemoryTechLeadRunRecordStore:
    """Run history for a process with no durable home for it.

    The "No Nulls" counterpart to :class:`SingleInstanceRunLedgerStore`: the
    activity owner never branches on whether history is persisted, so a
    composition without a state directory (tests, a one-shot CLI) still gets a
    real, ordered, bounded history for the life of the process instead of a
    silently-dropping stub.

    Inspection becomes permanently unknown after any eviction. Compositions
    replacing an unavailable durable store select ``history_complete=False``;
    a fresh isolated in-memory history can explicitly retain complete evidence.

    Locked for the same reason the single-instance ledger is: the tick thread
    writes while the dashboard thread reads.
    """

    def __init__(self, *, capacity: int = 200, history_complete: bool = True) -> None:
        if capacity < 1:
            raise ValueError("Run history capacity must be positive")
        self._history_complete = history_complete
        self._capacity = capacity
        self._records: list[TechLeadRunRecord] = []
        self._lock = threading.Lock()

    def open_run(self, record: TechLeadRunRecord) -> None:
        with self._lock:
            self._records = [
                existing
                for existing in self._records
                if not _is_run(existing, record.run_id, record.session_name)
            ]
            self._records.append(record)
            overflow = len(self._records) - self._capacity
            if overflow > 0:
                self._history_complete = False
                # Oldest-first by start time, so the bound drops the least
                # interesting rows rather than whichever arrived first.
                self._records.sort(key=lambda item: item.started_at)
                del self._records[:overflow]

    def conclude_run(
        self,
        *,
        run_id: str,
        session_name: str,
        phase: TechLeadRunPhase,
        ended_at: datetime,
        detail: str = "",
        findings: int = 0,
        proposals: int = 0,
        artifacts: Optional[TechLeadRunArtifacts] = None,
        delivery_outcome: TechLeadDeliveryOutcome = TechLeadDeliveryOutcome.LEGACY,
    ) -> None:
        with self._lock:
            for index, existing in enumerate(self._records):
                if _is_run(existing, run_id, session_name):
                    if existing.phase.is_terminal:
                        # The once-only guard the SQLite predicate applies, here
                        # too: a publish retry re-enters completion for the same
                        # session run, and a second conclusion would overwrite
                        # the first verdict with whatever the retry saw.
                        return
                    self._records[index] = existing.concluded(
                        phase=phase,
                        ended_at=ended_at,
                        detail=detail,
                        findings=findings,
                        proposals=proposals,
                        artifacts=artifacts,
                        delivery_outcome=delivery_outcome,
                    )
                    return

    def forget_artifacts(self, locations: "Sequence[Path]") -> None:
        retired = {Path(location) for location in locations}
        if not retired:
            return
        with self._lock:
            self._records = [
                replace(record, artifacts=None)
                if record.artifacts is not None and record.artifacts.location in retired
                else record
                for record in self._records
            ]

    def inspect_delivery_evidence(self) -> TechLeadDeliveryEvidence:
        with self._lock:
            records = tuple(self._records)
            complete = self._history_complete and all(row.delivery_known for row in records)
        if not complete:
            return TechLeadDeliveryEvidence(history_state=DeliveryHistoryState.INCOMPLETE)
        delivered = max(
            (
                delivery_time(row.ended_at)
                for row in records
                if row.delivered and row.ended_at is not None
            ),
            default=None,
        )
        starts = [
            delivery_time(row.started_at)
            for row in records
            if row.phase is not TechLeadRunPhase.WITHDRAWN
            and (delivered is None or delivery_time(row.started_at) > delivered)
        ]
        return TechLeadDeliveryEvidence(
            last_delivered_at=delivered,
            first_undelivered_at=min(starts, default=None),
            latest_started_at=max(starts, default=None),
            runs_without_delivery=len(starts),
        )

    def recent(self, *, limit: int) -> tuple[TechLeadRunRecord, ...]:
        with self._lock:
            ordered = sorted(
                self._records, key=lambda item: item.started_at, reverse=True
            )
        return tuple(ordered[:limit])


def _is_run(record: TechLeadRunRecord, run_id: str, session_name: str) -> bool:
    return record.run_id == run_id and record.session_name == session_name


__all__ = [
    "NO_TECH_LEAD_RUN_HISTORY",
    "InMemoryTechLeadRunRecordStore",
    "TechLeadDeliveryHistoryReader",
    "TechLeadRunHistoryReader",
    "TechLeadRunRecordStore",
]
