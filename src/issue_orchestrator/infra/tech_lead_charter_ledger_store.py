"""SQLite adapter for the ``TechLeadCharterLedger`` port (#7330).

The charter decision ledger lives in the tech-lead authority database, next to
the proposal-op ledger whose approval and decline it links back to, and shares
that store's connection, write lock and transaction boundary (the store is the
composition-root owner of the file). The full record is stored as JSON; the
columns a reader filters on are denormalized beside it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable, Iterable, Sequence

from ..domain.tech_lead_charter import CharterRole
from ..domain.tech_lead_charter_decisions import (
    CharterExecutionLink,
    CharterProposalLifecycle,
    TechLeadCharterDecision,
)
from ..ports.tech_lead_charter_ledger import (
    check_read_limit,
    keep_linked_lifecycle,
    linked_execution,
    links_to_proposal,
)

_UPSERT = (
    "INSERT INTO tech_lead_charter_decisions (decision_id, run_id, action_id,"
    " anchor_issue_number, target_number, proposal_issue_number, role,"
    " decided_at, record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(decision_id) DO UPDATE SET run_id = excluded.run_id,"
    " action_id = excluded.action_id,"
    " anchor_issue_number = excluded.anchor_issue_number,"
    " target_number = excluded.target_number,"
    " proposal_issue_number = excluded.proposal_issue_number,"
    " role = excluded.role, decided_at = excluded.decided_at,"
    " record = excluded.record"
)


def _row_values(decision: TechLeadCharterDecision) -> tuple[object, ...]:
    return (
        decision.decision_id,
        decision.run_id,
        decision.action_id,
        decision.anchor_issue_number,
        decision.target_number,
        decision.proposal_issue_number,
        decision.role.value,
        decision.decided_at,
        json.dumps(decision.to_dict(), sort_keys=True),
    )


def _decode(rows: Iterable[sqlite3.Row]) -> tuple[TechLeadCharterDecision, ...]:
    return tuple(TechLeadCharterDecision.from_dict(json.loads(row["record"])) for row in rows)


#: Decisions ABOUT one issue (#7331). Mirrors
#: ``TechLeadCharterDecision.is_about_issue`` and filters BEFORE the limit; both
#: halves of the OR are index searches (``tech_lead_charter_decisions_target`` /
#: ``_anchor``), because the board runs it once per blocked card.
ABOUT_ISSUE_QUERY = (
    "SELECT record FROM tech_lead_charter_decisions WHERE target_number = ?"
    " OR (target_number IS NULL AND anchor_issue_number = ?)"
    " ORDER BY decided_at DESC, decision_id DESC LIMIT ?"
)


#: Remedies aimed at one issue that TOOK EFFECT, newest effect first (#7331).
#: Mirrors ``TechLeadCharterDecision.is_remedy`` / ``took_effect`` /
#: ``effect_at`` over the persisted record: an executed decision took effect
#: only once its applier's result linked back as applied (#7362). The target
#: index narrows it to that issue's rows first.
REMEDIES_ON_ISSUE_QUERY = (
    "SELECT record FROM tech_lead_charter_decisions WHERE target_number = ?"
    " AND json_extract(record, '$.binding') IN ('approvable', 'destructive')"
    " AND ((json_extract(record, '$.outcome') = 'executed'"
    " AND json_extract(record, '$.execution') = 'applied')"
    " OR json_extract(record, '$.lifecycle') = 'approved_applied')"
    " ORDER BY CASE WHEN json_extract(record, '$.lifecycle') = 'approved_applied'"
    " AND json_extract(record, '$.lifecycle_updated_at') IS NOT NULL"
    " THEN json_extract(record, '$.lifecycle_updated_at')"
    " WHEN json_extract(record, '$.execution') IS NOT NULL"
    " AND json_extract(record, '$.execution_at') IS NOT NULL"
    " THEN json_extract(record, '$.execution_at') ELSE decided_at END DESC,"
    " decision_id DESC LIMIT ?"
)


def _counted(keys: Iterable[tuple[str, ...]]) -> tuple[tuple[Any, ...], ...]:
    counts: dict[tuple[str, ...], int] = {}
    for key in keys:
        counts[key] = counts.get(key, 0) + 1
    return tuple((*key, n) for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


class SqliteTechLeadCharterLedger:
    """Durable charter decisions over the authority store's connection."""

    def __init__(
        self,
        *,
        connection: Callable[[], sqlite3.Connection],
        transaction: Callable[[], AbstractContextManager[sqlite3.Connection]],
    ) -> None:
        self._connection = connection
        self._transaction = transaction

    def record_decisions(self, decisions: Iterable[TechLeadCharterDecision]) -> None:
        with self._transaction() as tx:
            for decision in decisions:
                row = tx.execute(
                    "SELECT record FROM tech_lead_charter_decisions WHERE decision_id = ?",
                    (decision.decision_id,),
                ).fetchone()
                existing = (
                    TechLeadCharterDecision.from_dict(json.loads(row["record"]))
                    if row is not None
                    else None
                )
                tx.execute(_UPSERT, _row_values(keep_linked_lifecycle(existing, decision)))

    def link_proposal_outcome(
        self,
        *,
        run_id: str,
        action_id: str,
        proposal_issue_number: int,
        lifecycle: CharterProposalLifecycle,
        at: str,
    ) -> int:
        with self._transaction() as tx:
            candidates = _decode(
                tx.execute(
                    # Every record of the run: a coalesced sibling names the
                    # origin action only inside its record; links_to_proposal
                    # decides exactly which ones match.
                    "SELECT record FROM tech_lead_charter_decisions WHERE"
                    " run_id = ? OR proposal_issue_number = ?",
                    (run_id, proposal_issue_number),
                )
            )
            updated = 0
            for row in candidates:
                if not links_to_proposal(
                    row,
                    run_id=run_id,
                    action_id=action_id,
                    proposal_issue_number=proposal_issue_number,
                ):
                    continue
                linked = row.with_lifecycle(
                    lifecycle, at=at, proposal_issue_number=proposal_issue_number
                )
                tx.execute(_UPSERT, _row_values(linked))
                updated += 1
            return updated

    def link_execution_outcomes(
        self, links: Sequence[CharterExecutionLink], *, at: str
    ) -> int:
        with self._transaction() as tx:
            updated = 0
            for link in links:
                row = tx.execute(
                    "SELECT record FROM tech_lead_charter_decisions WHERE decision_id = ?",
                    (link.decision_id,),
                ).fetchone()
                linked = linked_execution(
                    TechLeadCharterDecision.from_dict(json.loads(row["record"]))
                    if row is not None
                    else None,
                    link,
                    at=at,
                )
                if linked is None:
                    continue
                tx.execute(_UPSERT, _row_values(linked))
                updated += 1
            return updated

    def list_for_issue(
        self, issue_number: int, *, limit: int = 100
    ) -> tuple[TechLeadCharterDecision, ...]:
        return _decode(
            self._connection().execute(
                "SELECT record FROM tech_lead_charter_decisions WHERE target_number = ?"
                " OR anchor_issue_number = ? OR proposal_issue_number = ?"
                " ORDER BY decided_at DESC, decision_id DESC LIMIT ?",
                (issue_number, issue_number, issue_number, check_read_limit(limit)),
            )
        )

    def list_about_issue(
        self, issue_number: int, *, limit: int = 100
    ) -> tuple[TechLeadCharterDecision, ...]:
        return _decode(
            self._connection().execute(
                ABOUT_ISSUE_QUERY, (issue_number, issue_number, check_read_limit(limit))
            )
        )

    def latest_triage_for_issue(self, issue_number: int) -> TechLeadCharterDecision | None:
        found = _decode(
            self._connection().execute(
                "SELECT record FROM tech_lead_charter_decisions WHERE target_number = ?"
                " AND json_extract(record, '$.triage_class') IS NOT NULL"
                " ORDER BY decided_at DESC, decision_id DESC LIMIT 1",
                (issue_number,),
            )
        )
        return found[0] if found else None

    def list_remedies_on_issue(
        self, issue_number: int, *, limit: int = 100
    ) -> tuple[TechLeadCharterDecision, ...]:
        return _decode(
            self._connection().execute(
                REMEDIES_ON_ISSUE_QUERY, (issue_number, check_read_limit(limit))
            )
        )

    def list_filed_as_proposal(
        self, proposal_issue_number: int, *, limit: int = 100
    ) -> tuple[TechLeadCharterDecision, ...]:
        return _decode(
            self._connection().execute(
                "SELECT record FROM tech_lead_charter_decisions"
                " WHERE proposal_issue_number = ?"
                " ORDER BY decided_at DESC, decision_id DESC LIMIT ?",
                (proposal_issue_number, check_read_limit(limit)),
            )
        )

    def list_for_role(
        self, role: CharterRole, *, limit: int = 100
    ) -> tuple[TechLeadCharterDecision, ...]:
        return _decode(
            self._connection().execute(
                "SELECT record FROM tech_lead_charter_decisions WHERE role = ?"
                " ORDER BY decided_at DESC, decision_id DESC LIMIT ?",
                (role.value, check_read_limit(limit)),
            )
        )

    def role_outcome_counts(self) -> tuple[tuple[str, str, int], ...]:
        """How many decisions each ``(role, outcome)`` holds, most first (#7490)."""
        return _counted(
            (decision.role.value, decision.outcome.value) for decision in self._all()
        )

    def effect_counts(self) -> tuple[tuple[str, str, str, str, int], ...]:
        """How many decisions each ``(role, action_kind, outcome, effect)`` holds,
        most first: what the charter allowed and what then became of it
        (``TechLeadCharterDecision.effect``) (#7490)."""
        return _counted(
            (d.role.value, d.action_kind, d.outcome.value, d.effect) for d in self._all()
        )

    def list_all(self) -> tuple[TechLeadCharterDecision, ...]:
        """Every decision, oldest decided first (#7490: the improver's window)."""
        return _decode(
            self._connection().execute(
                "SELECT record FROM tech_lead_charter_decisions"
                " ORDER BY decided_at ASC, decision_id ASC"
            )
        )

    def _all(self) -> tuple[TechLeadCharterDecision, ...]:
        """Every decision, decoded, so counts read the domain's fields rather
        than JSON paths that could drift from them."""
        return _decode(self._connection().execute("SELECT record FROM tech_lead_charter_decisions"))

    def list_recent(self, *, limit: int = 100) -> tuple[TechLeadCharterDecision, ...]:
        return _decode(
            self._connection().execute(
                "SELECT record FROM tech_lead_charter_decisions"
                " ORDER BY decided_at DESC, decision_id DESC LIMIT ?",
                (check_read_limit(limit),),
            )
        )
