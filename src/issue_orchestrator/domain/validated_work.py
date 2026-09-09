"""Identity and admission contracts for retained validated work (#6914)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import cast

from .models import RequestedAction
from .review_exchange_summary import ReviewExchangeTerminalState
from .session_run import SessionRunIdentity

IDENTITY_SCHEMA_VERSION = 1


def require_text(value: str, name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be non-empty text")


def require_sha(value: str, *, size: int = 40) -> None:
    if type(value) is not str or re.fullmatch("[0-9a-f]{%d}" % size, value) is None:
        raise ValueError("hash must be full lowercase hexadecimal")


def require_positive(value: int, name: str, *, minimum: int = 1) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def canonical_json(value: object) -> str:
    return json.dumps(
        _normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _normalize(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and type(value) is not type:
        return {f.name: _normalize(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(k): _normalize(v)
            for k, v in cast(Mapping[object, object], value).items()
        }
    if isinstance(value, (tuple, list)):
        return [_normalize(v) for v in cast(tuple[object, ...], value)]
    return value


def canonical_record_id(key: ValidatedWorkKey) -> str:
    return (
        f"r{IDENTITY_SCHEMA_VERSION}:"
        + hashlib.sha256(canonical_json(key).encode()).hexdigest()
    )


def canonical_evidence_id(identity: ValidatedWorkIdentity) -> str:
    return (
        f"e{identity.schema_version}:"
        + hashlib.sha256(canonical_json(identity).encode()).hexdigest()
    )


def canonical_lineage_key(key: ValidatedWorkKey) -> str:
    payload = (key.repo_slug, key.issue_number, key.branch_name)
    return (
        f"l{IDENTITY_SCHEMA_VERSION}:"
        + hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    )


class ValidatedWorkState(StrEnum):
    QUEUED = "queued"  # recovery admitted automatically; drain will execute
    PARKED = "parked"  # durable, awaiting approval (gated op or operator)
    PUBLISHING = "publishing"  # admitted to the publisher; submission in flight
    RECOVERED = "recovered"  # published + review routed; resolved
    FAILED = "failed"  # fail-closed; artifacts preserved; UNRESOLVED
    ABANDONED = "abandoned"  # operator explicitly accepted the loss; resolved


class RemoteBaselineStatus(StrEnum):
    """Whether a missing remote SHA is authoritative absence or missing evidence."""

    OBSERVED = "observed"
    UNOBSERVED = "unobserved"


# Two DIFFERENT state sets. Conflating "resting" with "resolved" is the bug.

UNRESOLVED_STATES = frozenset(
    {  # work still exists and is not safe to lose
        ValidatedWorkState.QUEUED,
        ValidatedWorkState.PARKED,
        ValidatedWorkState.PUBLISHING,
        ValidatedWorkState.FAILED,  # <- FAILED IS UNRESOLVED, not "terminal"
    }
)

RESOLVED_STATES = frozenset(
    {  # nothing is at risk; reset/teardown may proceed
        ValidatedWorkState.RECOVERED,
        ValidatedWorkState.ABANDONED,
    }
)

# There is deliberately NO "none" state. "No completed+validated work at this edge"
# is not a property of a record — it is the ABSENCE of records, which an empty
# ValidatedWorkDispositionBatch says directly (§2.2). A `NONE` member would be a
# disposition of nothing, carrying no record_id and no key, and every consumer
# would have to special-case it out of a collection whose whole purpose is that
# each member names a unit of work. Every state here is persisted; every
# disposition names a row.
assert UNRESOLVED_STATES | RESOLVED_STATES == set(ValidatedWorkState)
assert not (UNRESOLVED_STATES & RESOLVED_STATES)

# There is deliberately no third "open" set. Uniqueness is not state-scoped:
# `record_id` is the table's primary key (§4.1), so there is at most ONE row per
# unit of work in every state. A state-scoped uniqueness rule is what let a rival
# row be admitted beside an unresolved FAILED one.


class ValidatedWorkFailure(StrEnum):
    """Precise, enumerable reasons. Never a free-text-only failure."""

    ESCROW_WRITE_FAILED = "escrow_write_failed"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_HASH_MISMATCH = "artifact_hash_mismatch"
    ARTIFACT_UNTRUSTED_PATH = "artifact_untrusted_path"
    VALIDATION_SHA_MISMATCH = "validation_sha_mismatch"  # head_sha unreachable
    WORKTREE_AHEAD_OF_VALIDATION = "worktree_ahead_of_validation"  # §1.1; parks
    ANCESTOR_OF_PENDING_HEAD = (
        "ancestor_of_pending_head"  # §2.1.4; parks behind a descendant
    )
    DIVERGENT_VALIDATED_HEADS = (
        "divergent_validated_heads"  # §2.1.4; parks for a choice
    )
    AWAITING_LINEAGE_PREDECESSOR = (
        "awaiting_lineage_predecessor"  # §2.1.4; parks behind PUBLISHING
    )
    REMOTE_BASELINE_UNPROVEN = (
        "remote_baseline_unproven"  # §2.1.4; parks rather than guess
    )
    AUTHORITY_SNAPSHOT_STALE = "authority_snapshot_stale"  # §8.1; approved facts moved
    DUPLICATE_OPEN_PR = "duplicate_open_pr"  # §4.4d; two marked PRs, never guess
    PUBLISHED_HEAD_LACKS_VALIDATED_WORK = (
        "published_head_lacks_validated_work"  # §3.5; parks
    )
    WORKSPACE_INTEGRITY = "workspace_integrity"  # #7017 detached HEAD etc.
    REF_PIN_LOST = "ref_pin_lost"
    PUBLISH_TARGET_MISMATCH = "publish_target_mismatch"  # §4.3 check 4 tripped
    REMOTE_DIVERGED = "remote_diverged"  # not a fast-forward
    REMOTE_HEAD_CHANGED = "remote_head_changed"  # third, unexpected sha
    REMOTE_UNREADABLE = "remote_unreadable"  # read failed != "absent"
    PR_CLOSED_OR_MERGED = "pr_closed_or_merged"
    PR_BRANCH_MISMATCH = "pr_branch_mismatch"
    ISSUE_UNREADABLE = "issue_unreadable"
    RUNTIME_ACTIVE = "runtime_active"
    PUSH_FAILED = "push_failed"
    SUBMISSION_LOST = "submission_lost"  # token has no live job
    REVIEW_ROUTING_FAILED = "review_routing_failed"


class ResolutionKind(StrEnum):
    """HOW a record became resolved. Recorded on the row; never inferred later."""

    PUBLISHED = "published"  # this record's own publication
    CONTAINED_IN_PUBLISHED_HEAD = "contained_in_published_head"  # §2.1.4 / §3.5
    OPERATOR_ABANDONED = "operator_abandoned"


class LineageRole(StrEnum):
    """This record's position among the validated heads of one issue+branch (§2.1.4)."""

    HEAD = "head"  # the only drainable role
    ANCESTOR = "ancestor"  # contained by a HEAD; parks, resolves when the HEAD lands
    DIVERGENT = (
        "divergent"  # not comparable with the others; parks for an explicit choice
    )
    PENDING = (
        "pending"  # admitted behind a PUBLISHING sibling; classified when it resolves
    )


class ReviewDisposition(StrEnum):
    ROUTE_TO_PR_REVIEW = "route_to_pr_review"  # normal review discovery on new head
    RESUME_REVIEW = "resume_review"  # PR already under review; update head
    EXCHANGE_APPROVED = "exchange_approved"  # exchange reached OK/REVIEWER_OK


class ArtifactSlot(StrEnum):
    """The fixed vocabulary of escrowed artifacts. A slot, not a path."""

    COMPLETION = "completion"
    VALIDATION = "validation"
    EXCHANGE_SUMMARY = "exchange_summary"


class EvidenceRole(StrEnum):
    CURRENT = "current"
    ATTACHED = "attached"
    SUPERSEDED = "superseded"


class PublicationProvenance(StrEnum):
    PUSHED_BY_OWNER = "pushed_by_owner"
    OBSERVED_MERGE = "observed_merge"


class FinalizationPhase(StrEnum):
    NOT_STARTED = "not_started"
    REVIEW_ROUTED = "review_routed"
    RECOVERY_CLEARED = "recovery_cleared"
    COMPLETE = "complete"


class DispositionPhase(StrEnum):
    PRE_SUBMISSION = "pre_submission"
    RECONCILING = "reconciling"


class PublishValidatedHeadStatus(StrEnum):
    PUBLISHED = "published"
    ALREADY_AT_TARGET = "already_at_target"
    REJECTED = "rejected"
    DIVERGED = "diverged"
    TRANSIENT_FAILURE = "transient_failure"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class AdmittedArtifact:
    """One admitted artifact. IDENTITY-BEARING — every field enters evidence_id."""

    slot: ArtifactSlot
    sha256: str  # lowercase hex
    byte_size: int

    def __post_init__(self) -> None:
        if type(self.slot) is not ArtifactSlot:
            raise ValueError("artifact requires a typed slot")
        require_sha(self.sha256, size=64)
        require_positive(self.byte_size, "byte_size", minimum=0)


@dataclass(frozen=True, slots=True)
class ValidatedWorkKey:
    """WHICH WORK this is: one commit, on one branch, of one issue, in one repo."""

    repo_slug: str
    issue_number: int
    branch_name: str
    validated_head_sha: str  # == validation record head_sha (§1.1)

    def __post_init__(self) -> None:
        require_text(self.repo_slug, "repo_slug")
        require_text(self.branch_name, "branch_name")
        require_positive(self.issue_number, "issue_number")
        require_sha(self.validated_head_sha)

    @property
    def record_id(self) -> str:
        """Stable primary key. Independent of which evidence carries the work."""
        return canonical_record_id(self)


@dataclass(frozen=True, slots=True)
class ValidatedWorkIdentity:
    """WHICH EVIDENCE this is: the key plus the admitted content that proves it."""

    schema_version: int  # bumped when the identity field set changes
    key: ValidatedWorkKey
    run_identity: SessionRunIdentity  # session_name, run_id, started_at
    completion_artifact: AdmittedArtifact
    validation_artifact: AdmittedArtifact
    exchange_summary_artifact: AdmittedArtifact | None
    requested_actions: tuple[RequestedAction, ...]
    review_disposition: ReviewDisposition
    exchange_terminal: ReviewExchangeTerminalState | None  # e.g. STOPPED/MAX_ROUNDS
    branch_binding_verified: bool  # False when HEAD was detached (#7017)
    reviewer_proof_digest: str | None  # required for EXCHANGE_APPROVED (§5)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != IDENTITY_SCHEMA_VERSION
        ):
            raise ValueError("unsupported identity schema")
        if (
            type(self.key) is not ValidatedWorkKey
            or type(self.run_identity) is not SessionRunIdentity
        ):
            raise ValueError("identity requires typed work and run identities")
        if type(self.branch_binding_verified) is not bool:
            raise ValueError("branch binding must be a bool")
        if type(self.review_disposition) is not ReviewDisposition:
            raise ValueError("review disposition must be typed")
        self._validate_artifacts()
        if type(self.requested_actions) is not tuple or any(
            type(a) is not RequestedAction for a in self.requested_actions
        ):
            raise ValueError("actions must be a tuple of RequestedAction")
        if not {RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR}.intersection(
            self.requested_actions
        ):
            raise ValueError("validated work must request publication")
        object.__setattr__(
            self,
            "requested_actions",
            tuple(sorted(set(self.requested_actions), key=lambda a: a.value)),
        )
        if self.reviewer_proof_digest is not None:
            require_sha(self.reviewer_proof_digest, size=64)
        if (
            self.review_disposition is ReviewDisposition.EXCHANGE_APPROVED
            and self.reviewer_proof_digest is None
        ):
            raise ValueError("exchange approval requires reviewer proof")
        if (
            self.exchange_terminal is not None
            and type(self.exchange_terminal) is not ReviewExchangeTerminalState
        ):
            raise ValueError("exchange terminal must be typed")

    def _validate_artifacts(self) -> None:
        for artifact, slot in (
            (self.completion_artifact, ArtifactSlot.COMPLETION),
            (self.validation_artifact, ArtifactSlot.VALIDATION),
        ):
            if type(artifact) is not AdmittedArtifact or artifact.slot is not slot:
                raise ValueError("artifact must occupy its named slot")
        artifact = self.exchange_summary_artifact
        if artifact is not None and (
            type(artifact) is not AdmittedArtifact
            or artifact.slot is not ArtifactSlot.EXCHANGE_SUMMARY
        ):
            raise ValueError("exchange artifact must occupy exchange_summary slot")


@dataclass(frozen=True, slots=True)
class ValidatedWorkObservations:
    """Everything read from mutable state. NONE of it enters evidence_id."""

    captured_at: str  # ISO-8601 UTC
    worktree_head_sha: str  # issue worktree HEAD at capture; may move
    expected_remote_head_sha: str | None  # None means absent only when observed
    pr_number: int | None
    observed_blocking_labels: tuple[str, ...]  # exactly what this op may later clear
    admitted_from_paths: Mapping[ArtifactSlot, str]  # audit only, never re-read
    remote_baseline_status: RemoteBaselineStatus = RemoteBaselineStatus.UNOBSERVED

    def __post_init__(self) -> None:
        require_text(self.captured_at, "captured_at")
        require_sha(self.worktree_head_sha)
        if self.expected_remote_head_sha is not None:
            require_sha(self.expected_remote_head_sha)
        if type(self.remote_baseline_status) is not RemoteBaselineStatus:
            raise ValueError("remote baseline status must be typed")
        if (
            self.remote_baseline_status is RemoteBaselineStatus.UNOBSERVED
            and (self.expected_remote_head_sha is not None or self.pr_number is not None)
        ):
            raise ValueError("unobserved remote state cannot carry branch or PR authority")
        if self.pr_number is not None:
            require_positive(self.pr_number, "pr_number")
        if type(self.observed_blocking_labels) is not tuple:
            raise ValueError("labels must be immutable")
        for label in self.observed_blocking_labels:
            require_text(label, "label")
        for slot, path in self.admitted_from_paths.items():
            if type(slot) is not ArtifactSlot:
                raise ValueError("audit paths require artifact slots")
            require_text(path, "audit path")
        object.__setattr__(
            self,
            "admitted_from_paths",
            MappingProxyType(dict(self.admitted_from_paths)),
        )


@dataclass(frozen=True, slots=True)
class ValidatedWorkEvidence:
    """Identity plus the mutable observations a recovery reconciles against."""

    identity: ValidatedWorkIdentity
    observations: ValidatedWorkObservations

    def __post_init__(self) -> None:
        if (
            type(self.identity) is not ValidatedWorkIdentity
            or type(self.observations) is not ValidatedWorkObservations
        ):
            raise ValueError("evidence requires typed identity and observations")

    @property
    def evidence_id(self) -> str:
        """Canonical identity hash. Depends on `identity` only."""
        return canonical_evidence_id(self.identity)

    @property
    def record_id(self) -> str:
        return self.identity.key.record_id
