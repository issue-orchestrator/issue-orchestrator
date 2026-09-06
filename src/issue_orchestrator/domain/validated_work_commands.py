"""Typed capture, approval and batch results; no lifecycle implementation."""

from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar
from .issue_run_evidence import IssueRunEvidence
from .validated_work import (
    ValidatedWorkKey,
    ValidatedWorkState,
    ValidatedWorkFailure,
    LineageRole,
    UNRESOLVED_STATES,
    require_text,
    require_sha,
    require_positive,
)


class DispositionInitiator(StrEnum):
    """WHO asked. Recorded on the row; never widens what the owner will do."""

    AUTOMATIC = "automatic"  # terminate_issue_runtime, at the boundary
    TECH_LEAD = "tech_lead"  # approved recover_validated_work gated op (§8.1)
    OPERATOR = "operator"  # Control Center command (§8.4)


@dataclass(frozen=True, slots=True)
class AutomaticCaptureCommand:
    """Capture at a terminal boundary. Carries the runs, not a hint of them."""

    issue_number: int
    reason: str  # the terminate_issue_runtime reason string
    run_evidence: IssueRunEvidence  # §2.5 — required; proves what was considered

    initiator: ClassVar[DispositionInitiator] = DispositionInitiator.AUTOMATIC

    def __post_init__(self) -> None:
        if self.run_evidence.issue_number != self.issue_number:
            raise ValueError("run evidence must belong to the issue being terminated")


@dataclass(frozen=True, slots=True)
class ValidatedWorkAuthoritySnapshot:
    """The facts an approval was given AGAINST. Immutable once approved."""

    record_id: str
    evidence_id: str
    observation_revision: int  # §2.1.1; bumped by every refresh
    validated_head_sha: str
    branch_name: str
    repo_slug: str
    issue_number: int
    pr_number: int | None  # the PR the approver saw
    expected_remote_head_sha: str | None  # the remote baseline the approver saw

    def __post_init__(self) -> None:
        key = ValidatedWorkKey(
            self.repo_slug, self.issue_number, self.branch_name, self.validated_head_sha
        )
        if self.record_id != key.record_id:
            raise ValueError("authority must bind the canonical work identity")
        require_text(self.evidence_id, "evidence_id")
        require_positive(self.observation_revision, "observation_revision", minimum=0)
        if self.pr_number is not None:
            require_positive(self.pr_number, "pr_number")
        if self.expected_remote_head_sha is not None:
            require_sha(self.expected_remote_head_sha)


@dataclass(frozen=True, slots=True)
class StoredEvidenceCommand:
    """Recover or re-submit evidence that is ALREADY durable. Never captures."""

    issue_number: int
    reason: str
    initiator: DispositionInitiator  # TECH_LEAD or OPERATOR only
    evidence_id: str  # non-empty, always
    actor: str  # operator identity or approved tech-lead op id
    authority: ValidatedWorkAuthoritySnapshot  # what was approved; checked for equality

    def __post_init__(self) -> None:
        if type(self.initiator) is not DispositionInitiator:
            raise ValueError("initiator must be typed")
        require_text(self.actor, "actor")
        require_text(self.reason, "reason")
        if self.initiator is DispositionInitiator.AUTOMATIC:
            raise ValueError(
                "stored-evidence recovery is never the automatic initiator"
            )
        if not self.evidence_id:
            raise ValueError("stored-evidence recovery requires an evidence_id")
        if not self.actor:
            raise ValueError("stored-evidence recovery requires an actor")
        if self.authority.evidence_id != self.evidence_id:
            raise ValueError("the approval must name the evidence it is executing")
        if self.authority.issue_number != self.issue_number:
            raise ValueError("the approval must name the issue it is executing")


ValidatedWorkDispositionCommand = AutomaticCaptureCommand | StoredEvidenceCommand


@dataclass(frozen=True, slots=True)
class OperatorResolution:
    """The ONLY way unresolved work becomes safe to lose. Never inferred."""

    actor: str  # authenticated operator identity; never an agent or op id
    reason: str  # non-empty; recorded verbatim in the durable row
    resolved_at: str  # ISO-8601 UTC

    def __post_init__(self) -> None:
        require_text(self.actor, "actor")
        require_text(self.reason, "reason")
        require_text(self.resolved_at, "resolved_at")


@dataclass(frozen=True, slots=True)
class AbandonValidatedWorkCommand:
    """Accept loss of exactly the evidence and observations the operator saw."""

    authority: ValidatedWorkAuthoritySnapshot  # rendered snapshot, echoed unchanged
    actor: str  # authenticated operator, supplied by handler
    reason: str  # operator-supplied, non-empty

    def __post_init__(self) -> None:
        if not self.actor.strip() or not self.reason.strip():
            raise ValueError("abandonment requires an operator and a reason")
        if not self.authority.record_id or not self.authority.evidence_id:
            raise ValueError("abandonment requires the exact rendered authority")


@dataclass(frozen=True, slots=True)
class ValidatedWorkDisposition:
    """The disposition OF ONE RECORD. It always names the work it disposes."""

    record_id: str  # required, non-empty; the durable row's key
    key: ValidatedWorkKey  # repo, issue, branch, validated head
    evidence_id: str  # required, non-empty; the CURRENT evidence
    state: ValidatedWorkState  # every state names a persisted row
    lineage_role: LineageRole
    reason: str
    failure: ValidatedWorkFailure | None = None
    pr_number: int | None = None
    published_head_sha: str | None = None
    resolution: OperatorResolution | None = None

    @property
    def unresolved(self) -> bool:
        """True while work still exists that must not be destroyed."""
        return self.state in UNRESOLVED_STATES

    def __post_init__(self) -> None:
        if (
            type(self.state) is not ValidatedWorkState
            or type(self.lineage_role) is not LineageRole
        ):
            raise ValueError("disposition states and roles must be typed")
        if self.failure is not None and type(self.failure) is not ValidatedWorkFailure:
            raise ValueError("failure must be enumerated")
        if self.published_head_sha is not None:
            require_sha(self.published_head_sha)
        if self.pr_number is not None:
            require_positive(self.pr_number, "pr_number")
        if (
            self.state is ValidatedWorkState.RECOVERED
            and self.published_head_sha is None
        ):
            raise ValueError("recovered work requires its published head")
        self._validate_resolution()

    def _validate_resolution(self) -> None:
        # fail-closed shape rules
        if not self.record_id or not self.evidence_id:
            raise ValueError("a disposition must name its record and its evidence")
        if self.record_id != self.key.record_id:
            raise ValueError("record_id must be the canonical id of the key it carries")
        if self.state is ValidatedWorkState.FAILED and self.failure is None:
            raise ValueError("FAILED disposition requires an enumerated failure")
        if self.state is ValidatedWorkState.ABANDONED and self.resolution is None:
            raise ValueError("ABANDONED requires an explicit OperatorResolution")
        if (
            self.state is not ValidatedWorkState.ABANDONED
            and self.resolution is not None
        ):
            raise ValueError("only ABANDONED carries an OperatorResolution")


@dataclass(frozen=True, slots=True)
class ValidatedWorkDispositionBatch:
    """EVERY disposition one termination produced. One per distinct ValidatedWorkKey."""

    issue_number: int
    dispositions: tuple[ValidatedWorkDisposition, ...]  # empty == no work found
    reason: str  # why, especially when empty

    @classmethod
    def no_work(cls, issue_number: int, reason: str) -> "ValidatedWorkDispositionBatch":
        """The explicit no-work result. Reads at call sites as what it is."""
        return cls(issue_number=issue_number, dispositions=(), reason=reason)

    def __post_init__(self) -> None:
        require_positive(self.issue_number, "issue_number")
        require_text(self.reason, "reason")
        if type(self.dispositions) is not tuple or any(
            type(d) is not ValidatedWorkDisposition for d in self.dispositions
        ):
            raise ValueError("batch requires an immutable tuple of dispositions")
        record_ids = [d.record_id for d in self.dispositions]
        if len(set(record_ids)) != len(record_ids):
            raise ValueError("one disposition per unit of work: duplicate record_id")
        for d in self.dispositions:
            if d.key.issue_number != self.issue_number:
                raise ValueError("every member must belong to the terminated issue")

    @property
    def found_work(self) -> bool:
        return bool(self.dispositions)

    @property
    def unresolved(self) -> bool:
        """True while ANY member still holds work that must not be destroyed."""
        return any(d.unresolved for d in self.dispositions)

    @property
    def unresolved_dispositions(self) -> tuple[ValidatedWorkDisposition, ...]:
        return tuple(d for d in self.dispositions if d.unresolved)
