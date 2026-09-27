"""Port for the persisted per-action charter decision ledger (#7330).

Two seams:

* :class:`TechLeadCharterDecisionReader` — the READ port a view model builds on
  (#7331): by issue, by role, and most recent. It returns the records exactly
  as decided; nothing here recomputes a charter.
* :class:`TechLeadCharterLedger` — the orchestrator's write side: record the
  decisions of a planned completion (idempotent per decision id), and link a
  gated proposal's approval or decline back to the decision that filed it.

The durable implementation lives in ``infra/tech_lead_charter_ledger_store.py``;
:class:`InMemoryTechLeadCharterLedger` is the test double.
"""

from __future__ import annotations

from dataclasses import replace
from threading import Lock
from typing import Iterable, Protocol

from ..domain.tech_lead_charter import CharterRole
from ..domain.tech_lead_charter_decisions import (
    CharterProposalLifecycle,
    TechLeadCharterDecision,
)

#: Upper bound on one read, so a reader cannot ask for an unbounded scan.
MAX_CHARTER_DECISION_READ = 500


def check_read_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_CHARTER_DECISION_READ:
        raise ValueError(
            f"charter decision read limit must be 1..{MAX_CHARTER_DECISION_READ},"
            f" got {limit}"
        )
    return limit


class TechLeadCharterDecisionReader(Protocol):
    """Read-only view of recorded charter decisions, newest first."""

    def list_for_issue(
        self, issue_number: int, *, limit: int = 100
    ) -> tuple[TechLeadCharterDecision, ...]:
        """Decisions that target, anchor on, or were filed as *issue_number*."""
        ...

    def list_for_role(
        self, role: CharterRole, *, limit: int = 100
    ) -> tuple[TechLeadCharterDecision, ...]:
        """Decisions classified under *role*."""
        ...

    def list_recent(self, *, limit: int = 100) -> tuple[TechLeadCharterDecision, ...]:
        """The most recently decided records."""
        ...


class TechLeadCharterLedger(TechLeadCharterDecisionReader, Protocol):
    """The durable write side, owned by the orchestrator."""

    def record_decisions(self, decisions: Iterable[TechLeadCharterDecision]) -> None:
        """Upsert by ``decision_id``; a replay never regresses a linked lifecycle.

        A completion can be planned again after a crash. The replay's verdict
        replaces the earlier one (it is what the replayed plan applies), but a
        lifecycle already linked to the record is kept.
        """
        ...

    def link_proposal_outcome(
        self,
        *,
        run_id: str,
        action_id: str,
        proposal_issue_number: int,
        lifecycle: CharterProposalLifecycle,
        at: str,
    ) -> int:
        """Record what became of a gated proposal; returns the rows updated.

        Matches the decision that filed it — ``(run_id, action_id)`` — and any
        decision recorded as a re-proposal onto the same proposal issue.
        """
        ...


def _newest_first(
    rows: Iterable[TechLeadCharterDecision], limit: int
) -> tuple[TechLeadCharterDecision, ...]:
    ordered = sorted(rows, key=lambda row: (row.decided_at, row.decision_id), reverse=True)
    return tuple(ordered[: check_read_limit(limit)])


def keep_linked_lifecycle(
    existing: TechLeadCharterDecision | None, incoming: TechLeadCharterDecision
) -> TechLeadCharterDecision:
    """The upsert rule shared by every implementation (see ``record_decisions``).

    An unchanged decision keeps its original ``decided_at`` — a lane that
    re-states the same verdict every tick must not keep re-dating it — and a
    lifecycle the proposal lifecycle already linked is never regressed.
    """
    if existing is None:
        return incoming
    if replace(existing, decided_at=incoming.decided_at, lifecycle=incoming.lifecycle,
               lifecycle_updated_at=incoming.lifecycle_updated_at,
               proposal_issue_number=incoming.proposal_issue_number) == incoming:
        incoming = replace(
            incoming,
            decided_at=existing.decided_at,
            lifecycle_updated_at=(
                existing.lifecycle_updated_at
                if existing.lifecycle == incoming.lifecycle
                else incoming.lifecycle_updated_at
            ),
        )
    if existing.lifecycle in (None, CharterProposalLifecycle.AWAITING_APPROVAL):
        return incoming
    return replace(
        incoming,
        lifecycle=existing.lifecycle,
        lifecycle_updated_at=existing.lifecycle_updated_at,
        proposal_issue_number=existing.proposal_issue_number,
    )


def links_to_proposal(
    row: TechLeadCharterDecision,
    *,
    run_id: str,
    action_id: str,
    proposal_issue_number: int,
) -> bool:
    """True for a still-awaiting decision filed as, or re-proposed onto, this issue.

    A decision whose lifecycle is already terminal is never re-linked: a crash
    between finalizing an approved op and discarding its row would otherwise let
    the later terminal-cleanup pass relabel an applied proposal "declined".
    """
    if row.lifecycle is not CharterProposalLifecycle.AWAITING_APPROVAL:
        return False
    return (row.run_id == run_id and row.action_id == action_id) or (
        row.proposal_issue_number == proposal_issue_number
    )


class InMemoryTechLeadCharterLedger:
    """Process-local ledger for tests and composition without a state dir."""

    def __init__(self) -> None:
        self._rows: dict[str, TechLeadCharterDecision] = {}
        self._lock = Lock()

    def record_decisions(self, decisions: Iterable[TechLeadCharterDecision]) -> None:
        with self._lock:
            for decision in decisions:
                self._rows[decision.decision_id] = keep_linked_lifecycle(
                    self._rows.get(decision.decision_id), decision
                )

    def link_proposal_outcome(
        self,
        *,
        run_id: str,
        action_id: str,
        proposal_issue_number: int,
        lifecycle: CharterProposalLifecycle,
        at: str,
    ) -> int:
        updated = 0
        with self._lock:
            for key, row in list(self._rows.items()):
                if links_to_proposal(
                    row,
                    run_id=run_id,
                    action_id=action_id,
                    proposal_issue_number=proposal_issue_number,
                ):
                    self._rows[key] = row.with_lifecycle(
                        lifecycle, at=at, proposal_issue_number=proposal_issue_number
                    )
                    updated += 1
        return updated

    def list_for_issue(
        self, issue_number: int, *, limit: int = 100
    ) -> tuple[TechLeadCharterDecision, ...]:
        with self._lock:
            rows = [
                row
                for row in self._rows.values()
                if issue_number
                in (row.target_number, row.anchor_issue_number, row.proposal_issue_number)
            ]
        return _newest_first(rows, limit)

    def list_for_role(
        self, role: CharterRole, *, limit: int = 100
    ) -> tuple[TechLeadCharterDecision, ...]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.role is role]
        return _newest_first(rows, limit)

    def list_recent(self, *, limit: int = 100) -> tuple[TechLeadCharterDecision, ...]:
        with self._lock:
            rows = list(self._rows.values())
        return _newest_first(rows, limit)
