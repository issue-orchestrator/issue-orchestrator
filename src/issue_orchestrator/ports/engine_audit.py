"""What ``io engine-audit`` reads, as narrow read-only ports (#7490).

Each port is the subset of an owning store's reads the audit needs, so the
audit gathers facts through the stores that own them, never through SQL that
duplicates their knowledge. Every one is satisfied by the store opened on a
snapshot of the engine's database, so none of them can reach the live file.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..domain.action_liveness import LivenessRow
from ..domain.tech_lead_charter_decisions import TechLeadCharterDecision
from ..domain.tech_lead_findings import PromotedFinding
from .action_liveness import PendingPause
from .issue import Issue
from .pending_work_claim_store import QuarantineRecord, UnreadableClaim, UnresolvedClaim
from .pull_request_tracker import PRInfo
from .timeline_store import TimelineRecord


@dataclass(frozen=True, slots=True)
class UnresolvedWorkRecord:
    record_id: str
    issue_number: int
    state: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ValidatedWorkCensus:
    """Every record counted by ``(state, resolution_kind)``, and the unresolved ones."""

    by_state_resolution: tuple[tuple[str, str, int], ...]
    unresolved: tuple[UnresolvedWorkRecord, ...]


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    issue_number: int
    record: TimelineRecord


class ValidatedWorkCensusReader(Protocol):
    def census(self) -> ValidatedWorkCensus: ...


class ActionLivenessAuditReader(Protocol):
    def all_rows(self) -> tuple[LivenessRow, ...]: ...

    def pending_pauses(self) -> tuple[PendingPause, ...]: ...


class CharterAuditReader(Protocol):
    def role_outcome_counts(self) -> tuple[tuple[str, str, int], ...]: ...

    def list_recent(self, *, limit: int = 100) -> tuple[TechLeadCharterDecision, ...]: ...


class PromotionAuditReader(Protocol):
    def list_promotions(self) -> tuple[PromotedFinding, ...]: ...


class ClaimAuditReader(Protocol):
    def list_unresolved_claims(self) -> tuple[UnresolvedClaim, ...]: ...

    def list_unreadable_claims(self) -> tuple[UnreadableClaim, ...]: ...

    def list_quarantines(self) -> tuple[QuarantineRecord, ...]: ...


class TimelineAuditReader(Protocol):
    def events_between(self, start: datetime, end: datetime) -> Iterable[TimelineEvent]:
        """Every issue's events from ``start`` to ``end`` inclusive, oldest first."""
        ...


class OpenWorkHost(Protocol):
    """The two repository listings the audit reads: open issues and open PRs.

    Both must be complete or raise (a rate limit raises
    :class:`~.repository_host.RepositoryHostRateLimitedError`).
    """

    def list_issues(
        self,
        labels: list[str] | None = None,
        milestone: str | None = None,
        state: str = "open",
        limit: int = 100,
        required_stable_ids: set[str] | None = None,
        *,
        exhaustive: bool = False,
    ) -> Sequence[Issue]: ...

    def list_open_prs_complete(self) -> Sequence[PRInfo]: ...


__all__ = [
    "ActionLivenessAuditReader",
    "CharterAuditReader",
    "ClaimAuditReader",
    "OpenWorkHost",
    "PromotionAuditReader",
    "TimelineAuditReader",
    "TimelineEvent",
    "UnresolvedWorkRecord",
    "ValidatedWorkCensus",
    "ValidatedWorkCensusReader",
]
