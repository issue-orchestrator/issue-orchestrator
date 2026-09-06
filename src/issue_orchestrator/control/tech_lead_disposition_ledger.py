"""Durable wait phases and positive recovery evidence for the stuck sweep."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from ..ports import RepositoryHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore

logger = logging.getLogger(__name__)


class StuckSweepDispositions(Protocol):
    def owned_issue_numbers(self) -> frozenset[int]: ...
    def incident_issue_numbers(self) -> frozenset[int]: ...
    def release_recovered(self, blocked: frozenset[int], observed: frozenset[int]) -> frozenset[int]: ...


class _NoDispositions:
    def owned_issue_numbers(self) -> frozenset[int]:
        return frozenset()

    def incident_issue_numbers(self) -> frozenset[int]:
        return frozenset()

    def release_recovered(self, blocked: frozenset[int], observed: frozenset[int]) -> frozenset[int]:
        return frozenset()


NO_TECH_LEAD_DISPOSITIONS: StuckSweepDispositions = _NoDispositions()


class TechLeadDispositionLedger:
    """Active ownership is finite; lapsed context cannot silently reactivate."""

    def __init__(self, *, authority: "TechLeadAuthorityStore",
        issue_state: Callable[[int], str | None], now: datetime) -> None:
        self._authority, self._issue_state, self._now = authority, issue_state, now

    def _read(self, number: int) -> str | None:
        try:
            state = self._issue_state(number)
        except Exception:
            logger.warning("Disposition issue #%d unreadable; retaining incident", number, exc_info=True)
            return "unknown"
        if state not in {"open", "closed", None, "unknown"}:
            raise ValueError(f"unexpected issue state: {state!r}")
        return state

    def incident_issue_numbers(self) -> frozenset[int]:
        return frozenset(row.issue_number for row in self._authority.list_dispositions()
            if row.phase != "recovered")

    def owned_issue_numbers(self) -> frozenset[int]:
        owned: set[int] = set()
        states: dict[int, str | None] = {}
        for row in self._authority.list_dispositions():
            if row.phase not in {"prepared", "waiting"}:
                continue
            expired = self._now >= row.reassess_at
            if not expired and row.tracker_issue_number not in states:
                states[row.tracker_issue_number] = self._read(row.tracker_issue_number)
            if expired or states.get(row.tracker_issue_number) in {"closed", None}:
                self._authority.transition_disposition(previous=row, disposition=replace(row, phase="reassess"))
            else:
                owned.add(row.issue_number)
        return frozenset(owned)

    def release_recovered(self, blocked: frozenset[int], observed: frozenset[int]) -> frozenset[int]:
        recovered: set[int] = set()
        for row in self._authority.list_dispositions():
            if row.phase == "recovered" or row.issue_number in blocked:
                continue
            if row.issue_number in observed or self._read(row.issue_number) in {"closed", None}:
                if self._authority.transition_disposition(previous=row, disposition=replace(row, phase="recovered", recovered_at=self._now.isoformat())):
                    recovered.add(row.issue_number)
        return frozenset(recovered)


def build_disposition_ledger(authority: "TechLeadAuthorityStore | None",
    repository_host: "RepositoryHost", now: float) -> StuckSweepDispositions:
    if authority is None:
        return NO_TECH_LEAD_DISPOSITIONS
    return TechLeadDispositionLedger(authority=authority,
        issue_state=repository_host.get_issue_state, now=datetime.fromtimestamp(now, timezone.utc))
