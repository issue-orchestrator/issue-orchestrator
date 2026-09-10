"""Immutable per-record entry authority and fresh issue facts for recovery."""

from dataclasses import dataclass
from enum import StrEnum

from .recovery_attempt import RecoveryAttemptPending, RecoveryAuthorityStale
from .validated_work import EvidenceRole, LineageRole, ValidatedWorkFailure, ValidatedWorkState, require_positive, require_text
from .validated_work_commands import ValidatedWorkAuthoritySnapshot
from .validated_work_store import ValidatedWorkRecord


class RecoveryIssueState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class RecoveryIssue:
    repo_slug: str
    number: int
    title: str
    state: RecoveryIssueState
    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        require_text(self.repo_slug, "repo_slug")
        require_positive(self.number, "issue number")
        require_text(self.title, "issue title")
        if type(self.state) is not RecoveryIssueState or type(self.labels) is not tuple:
            raise ValueError("recovery issue requires typed state and immutable labels")
        for label in self.labels:
            require_text(label, "issue label")

    def require_identity(self, repo_slug: str, issue_number: int) -> None:
        if self.repo_slug != repo_slug or self.number != issue_number:
            raise ValueError("fresh recovery issue differs from the requested repository/issue")

    def permits_recovery(self, pause_label: str) -> bool:
        return self.state is RecoveryIssueState.OPEN and pause_label not in self.labels


@dataclass(frozen=True, slots=True)
class RecoveryRecordRequest:
    record_id: str
    evidence_id: str
    approved: ValidatedWorkAuthoritySnapshot | None = None

    def __post_init__(self) -> None:
        require_text(self.record_id, "record_id")
        require_text(self.evidence_id, "evidence_id")
        if self.approved is not None and (self.approved.record_id != self.record_id
                or self.approved.evidence_id != self.evidence_id):
            raise ValueError("recovery approval names other retained work")

    def refusal(self, record: ValidatedWorkRecord) -> RecoveryAttemptPending | None:
        row, disposition = record.current_evidence, record.disposition
        if (disposition.record_id != self.record_id or row.evidence_id != self.evidence_id
                or row.role is not EvidenceRole.CURRENT or row.released_at):
            return self._stale_refusal(row.authority, "Recovery evidence is no longer current")
        if self.approved is not None and self.approved != row.authority:
            return self._stale_refusal(row.authority, "Recovery approval no longer matches retained facts")
        state = disposition.state
        if state is ValidatedWorkState.PUBLISHING:
            return None
        if state is ValidatedWorkState.QUEUED and disposition.lineage_role is LineageRole.HEAD:
            return None
        if state is ValidatedWorkState.PARKED and self.approved is not None:
            return None
        return RecoveryAttemptPending("Retained work is not authorized for publication")

    def _stale_refusal(
        self, current: ValidatedWorkAuthoritySnapshot, message: str
    ) -> RecoveryAttemptPending:
        stale = (
            RecoveryAuthorityStale(self.approved, current)
            if self.approved is not None
            else None
        )
        return RecoveryAttemptPending(
            stale.describe() if stale is not None else message,
            ValidatedWorkFailure.AUTHORITY_SNAPSHOT_STALE,
            stale,
        )
