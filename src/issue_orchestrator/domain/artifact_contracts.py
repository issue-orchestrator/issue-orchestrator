"""Typed artifact contract primitives.

These types name workflow artifacts before any UI route or timeline projection
tries to display them. A run directory can still be storage metadata, but an
operator-facing artifact should be a concrete typed reference with validated
evidence.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


class ArtifactContractViolation(ValueError):
    """Raised when a typed artifact contract cannot be constructed."""

    def __init__(self, contract: str, field: str, reason: str) -> None:
        self.contract = contract
        self.field = field
        self.reason = reason
        super().__init__(f"{contract}.{field}: {reason}")


def _require_positive_int(contract: str, field: str, value: object) -> None:
    # Strict identity rejects bool, which is a subclass of int.
    if type(value) is not int:
        raise ArtifactContractViolation(contract, field, "must be a positive integer")
    if value <= 0:
        raise ArtifactContractViolation(contract, field, "must be a positive integer")


def _require_non_empty_string(contract: str, field: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactContractViolation(contract, field, "must be a non-empty string")


def _coerce_path(contract: str, field: str, value: object) -> Path:
    if not isinstance(value, Path):
        raise ArtifactContractViolation(contract, field, "must be a pathlib.Path")
    return value


@dataclass(frozen=True, slots=True)
class IssueNumber:
    """Positive issue number carried by artifact scopes."""

    value: int

    def __post_init__(self) -> None:
        _require_positive_int(type(self).__name__, "value", self.value)


@dataclass(frozen=True, slots=True)
class ExchangeRunId:
    """Stable review-exchange run identity, not a filesystem path.

    This primitive intentionally constrains identity safety here, while leaving
    producer-specific run-id formats to the producer contracts that own them.
    """

    value: str

    def __post_init__(self) -> None:
        _require_non_empty_string(type(self).__name__, "value", self.value)
        if "/" in self.value or "\\" in self.value:
            raise ArtifactContractViolation(
                type(self).__name__,
                "value",
                "must be an identifier, not a path",
            )


@dataclass(frozen=True, slots=True)
class PositiveRoundIndex:
    """One-based review-exchange round index."""

    value: int

    def __post_init__(self) -> None:
        _require_positive_int(type(self).__name__, "value", self.value)


@dataclass(frozen=True, slots=True)
class PositiveAttemptIndex:
    """One-based role attempt index within a review-exchange round."""

    value: int

    def __post_init__(self) -> None:
        _require_positive_int(type(self).__name__, "value", self.value)


@dataclass(frozen=True, slots=True)
class AgentProvider:
    """Configured provider identity, e.g. ``claude-code`` or ``codex``."""

    value: str

    def __post_init__(self) -> None:
        _require_non_empty_string(type(self).__name__, "value", self.value)


class AgentRole(str, enum.Enum):
    """Agent role that produced or consumed an artifact."""

    CODER = "coder"
    REVIEWER = "reviewer"


class RenderMode(str, enum.Enum):
    """How a consumer should render a file-backed artifact."""

    TEXT = "text"
    JSON = "json"
    TERMINAL_RECORDING = "terminal_recording"
    CODEX_JSON_STREAM = "codex_json_stream"


@dataclass(frozen=True, slots=True)
class ExistingDirectory:
    """Filesystem evidence for a directory that must already exist."""

    path: Path

    def __post_init__(self) -> None:
        path = _coerce_path(type(self).__name__, "path", self.path)
        if not path.is_dir():
            raise ArtifactContractViolation(
                type(self).__name__,
                "path",
                f"directory not found: {path}",
            )


@dataclass(frozen=True, slots=True)
class ExistingFile:
    """Filesystem evidence for a file that must already exist."""

    path: Path

    def __post_init__(self) -> None:
        path = _coerce_path(type(self).__name__, "path", self.path)
        if not path.is_file():
            raise ArtifactContractViolation(
                type(self).__name__,
                "path",
                f"file not found: {path}",
            )


@dataclass(frozen=True, slots=True)
class ExistingNonEmptyFile(ExistingFile):
    """Filesystem evidence for a file that must exist and have content."""

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.path.stat().st_size <= 0:
            raise ArtifactContractViolation(
                type(self).__name__,
                "path",
                f"file is empty: {self.path}",
            )


@dataclass(frozen=True, slots=True)
class RunArtifactScope:
    """Artifact scoped to one run identity and its storage root."""

    issue_number: IssueNumber
    run_id: ExchangeRunId
    storage_root: ExistingDirectory


@dataclass(frozen=True, slots=True)
class ReviewExchangeArtifactScope:
    """Artifact scoped to a whole review-exchange run."""

    issue_number: IssueNumber
    exchange_run_id: ExchangeRunId
    exchange_dir: ExistingDirectory


@dataclass(frozen=True, slots=True)
class AgentTurnArtifactScope:
    """Artifact scoped to one agent turn inside a review exchange."""

    issue_number: IssueNumber
    exchange_run_id: ExchangeRunId
    round_index: PositiveRoundIndex
    attempt_index: PositiveAttemptIndex
    role: AgentRole
    provider: AgentProvider


@dataclass(frozen=True, slots=True)
class ValidationArtifactScope:
    """Artifact scoped to validation evidence for one run."""

    issue_number: IssueNumber
    run_id: ExchangeRunId
    run_dir: ExistingDirectory


# ---------------------------------------------------------------------------
# Validation outcome — discriminated union
# ---------------------------------------------------------------------------
#
# Historically the run manifest carried three independent Optionals
# (``validation_passed``, ``validation_status``, ``validation_reason``)
# updated via partial-merge ``update_manifest`` calls. The combination
# made it possible — and at one point real, see PR #6299 follow-up — to
# write ``{validation_status: "passed", validation_reason: "Validation
# failed for a949871f"}``: the passed-path writer omitted the reason
# field and a stale failure message survived the merge.
#
# These value types replace the loose triple. Constructors enforce the
# invariant ("passed" cannot carry a reason; "failed"/"retry" require a
# non-empty reason) so a stale-reason-on-success state is unrepresentable.
# Writers always set the whole outcome atomically.


@dataclass(frozen=True, slots=True)
class ValidationPassed:
    """Validation passed — no failure reason exists by construction."""

    kind: ClassVar[str] = "passed"


@dataclass(frozen=True, slots=True)
class ValidationFailed:
    """Validation failed terminally (retries exhausted or non-retryable)."""

    reason: str
    kind: ClassVar[str] = "failed"

    def __post_init__(self) -> None:
        _require_non_empty_string(type(self).__name__, "reason", self.reason)


@dataclass(frozen=True, slots=True)
class ValidationRetry:
    """Validation failed but the session has retries remaining."""

    reason: str
    kind: ClassVar[str] = "retry"

    def __post_init__(self) -> None:
        _require_non_empty_string(type(self).__name__, "reason", self.reason)


ValidationOutcome = ValidationPassed | ValidationFailed | ValidationRetry


def validation_outcome_from_manifest_fields(
    *,
    validation_passed: bool | None,
    validation_status: str | None,
    validation_reason: str | None,
) -> ValidationOutcome | None:
    """Reconstruct a typed outcome from the legacy flat manifest fields.

    Used by ``RunManifest.validation_outcome`` to derive the union from
    on-disk manifests written before this refactor. New writers go
    through ``validation_outcome_to_manifest_fields`` so the round-trip
    is lossless and consistent.

    Returns ``None`` when no outcome has been recorded yet (all three
    fields are None / absent).

    On encountering a logically inconsistent triple (e.g. status="passed"
    paired with a stale reason left behind by an earlier failure), the
    typed status field is the source of truth — we surface a
    ``ValidationPassed`` and the stale reason is dropped. This is the
    correct read-time behavior given the mismatch *is* the bug we're
    fixing; new writes won't produce it.
    """
    status = (validation_status or "").strip().lower()
    if status == "passed":
        return ValidationPassed()
    if status == "failed":
        return ValidationFailed(reason=validation_reason or "validation failed")
    if status == "retry":
        return ValidationRetry(reason=validation_reason or "validation failed")
    # Legacy fallback: status not set, but the boolean is.
    if validation_passed is True:
        return ValidationPassed()
    if validation_passed is False:
        return ValidationFailed(reason=validation_reason or "validation failed")
    return None


def validation_outcome_to_manifest_fields(
    outcome: ValidationOutcome,
) -> dict[str, object]:
    """Project a typed outcome into the three legacy flat fields.

    Always writes all three fields together so partial-merge
    ``update_manifest`` calls cannot leave a previous outcome's reason
    dangling on a fresh outcome's status. ``ValidationPassed``
    explicitly sets ``validation_reason`` to ``None`` to clear any
    prior stale value.
    """
    if isinstance(outcome, ValidationPassed):
        return {
            "validation_passed": True,
            "validation_status": "passed",
            "validation_reason": None,
        }
    if isinstance(outcome, ValidationFailed):
        return {
            "validation_passed": False,
            "validation_status": "failed",
            "validation_reason": outcome.reason,
        }
    # Exhaustive: only ValidationRetry remains.
    return {
        "validation_passed": False,
        "validation_status": "retry",
        "validation_reason": outcome.reason,
    }


# Manifest fields that ``update_manifest`` callers must NOT write
# directly — they are owned by the typed validation-outcome update API.
# Listed here so the runtime guard in ``FileSystemSessionOutput.update_manifest``
# can reject ad-hoc writes loudly.
RESERVED_VALIDATION_OUTCOME_FIELDS: frozenset[str] = frozenset(
    {"validation_passed", "validation_status", "validation_reason"}
)


ArtifactScope = (
    RunArtifactScope
    | ReviewExchangeArtifactScope
    | AgentTurnArtifactScope
    | ValidationArtifactScope
)


class ArtifactType(str, enum.Enum):
    """Stable artifact type names for producer/consumer contracts."""

    PROMPT = "prompt"
    TERMINAL_RECORDING = "terminal_recording"
    CHAPTER_SIDECAR = "chapter_sidecar"
    COMPLETION_RECORD = "completion_record"
    VALIDATION_RESULT = "validation_result"
    REVIEW_RESPONSE = "review_response"
    REVIEW_EXCHANGE_SUMMARY = "review_exchange_summary"


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """UI-agnostic file artifact reference."""

    artifact_type: ArtifactType
    label: str
    path: ExistingFile
    scope: ArtifactScope
    render_mode: RenderMode

    def __post_init__(self) -> None:
        _require_non_empty_string(type(self).__name__, "label", self.label)

    def to_manifest_artifact(self) -> dict[str, str]:
        """Render a JSON-safe manifest artifact payload."""
        return {
            "kind": self.artifact_type.value,
            "path": str(self.path.path),
            "render_mode": self.render_mode.value,
        }

    def to_timeline_artifact(self) -> dict[str, str]:
        """Render the legacy timeline artifact shape without guessing."""
        return {
            "type": self.artifact_type.value,
            "label": self.label,
            "value": str(self.path.path),
        }

    def to_event_artifact(self) -> dict[str, str]:
        """Render a producer event artifact reference."""
        return {
            "kind": self.artifact_type.value,
            "label": self.label,
            "path": str(self.path.path),
            "render_mode": self.render_mode.value,
        }


@dataclass(frozen=True, slots=True)
class _FileArtifact:
    """Base for named artifact wrappers that project to ``ArtifactRef``."""

    scope: ArtifactScope
    file: ExistingFile

    artifact_type: ClassVar[ArtifactType]
    label: ClassVar[str]
    render_mode: ClassVar[RenderMode]

    def to_ref(self) -> ArtifactRef:
        return ArtifactRef(
            artifact_type=self.artifact_type,
            label=self.label,
            path=self.file,
            scope=self.scope,
            render_mode=self.render_mode,
        )

    def to_manifest_artifact(self) -> dict[str, str]:
        return self.to_ref().to_manifest_artifact()

    def to_timeline_artifact(self) -> dict[str, str]:
        return self.to_ref().to_timeline_artifact()


@dataclass(frozen=True, slots=True)
class PromptArtifact(_FileArtifact):
    artifact_type: ClassVar[ArtifactType] = ArtifactType.PROMPT
    label: ClassVar[str] = "Prompt"
    render_mode: ClassVar[RenderMode] = RenderMode.TEXT
    file: ExistingNonEmptyFile


@dataclass(frozen=True, slots=True)
class TerminalRecordingArtifact(_FileArtifact):
    artifact_type: ClassVar[ArtifactType] = ArtifactType.TERMINAL_RECORDING
    label: ClassVar[str] = "Terminal Recording"
    render_mode: ClassVar[RenderMode] = RenderMode.TERMINAL_RECORDING
    scope: AgentTurnArtifactScope
    # Live turns can start before the PTY has emitted bytes; completed-turn
    # non-empty guarantees belong in a narrower completed-recording contract.
    file: ExistingFile


@dataclass(frozen=True, slots=True)
class ChapterSidecarArtifact(_FileArtifact):
    artifact_type: ClassVar[ArtifactType] = ArtifactType.CHAPTER_SIDECAR
    label: ClassVar[str] = "Replay Chapters"
    render_mode: ClassVar[RenderMode] = RenderMode.JSON
    scope: AgentTurnArtifactScope


@dataclass(frozen=True, slots=True)
class CompletionRecordArtifact(_FileArtifact):
    artifact_type: ClassVar[ArtifactType] = ArtifactType.COMPLETION_RECORD
    label: ClassVar[str] = "Completion Record"
    render_mode: ClassVar[RenderMode] = RenderMode.JSON


@dataclass(frozen=True, slots=True)
class ValidationResultArtifact(_FileArtifact):
    artifact_type: ClassVar[ArtifactType] = ArtifactType.VALIDATION_RESULT
    label: ClassVar[str] = "Validation Result"
    render_mode: ClassVar[RenderMode] = RenderMode.JSON
    scope: ValidationArtifactScope


@dataclass(frozen=True, slots=True)
class ReviewResponseArtifact(_FileArtifact):
    artifact_type: ClassVar[ArtifactType] = ArtifactType.REVIEW_RESPONSE
    label: ClassVar[str] = "Review Response"
    render_mode: ClassVar[RenderMode] = RenderMode.JSON
    scope: AgentTurnArtifactScope


@dataclass(frozen=True, slots=True)
class ReviewExchangeSummaryArtifact(_FileArtifact):
    artifact_type: ClassVar[ArtifactType] = ArtifactType.REVIEW_EXCHANGE_SUMMARY
    label: ClassVar[str] = "Review Exchange Summary"
    render_mode: ClassVar[RenderMode] = RenderMode.JSON
    scope: ReviewExchangeArtifactScope


@dataclass(frozen=True, slots=True)
class AgentTurnStarted:
    """Required artifacts known when one agent turn starts."""

    scope: AgentTurnArtifactScope
    prompt: PromptArtifact
    terminal_recording: TerminalRecordingArtifact
    chapters: ChapterSidecarArtifact

    def __post_init__(self) -> None:
        _require_matching_scope(type(self).__name__, self.scope, self.prompt.scope)
        _require_matching_scope(
            type(self).__name__,
            self.scope,
            self.terminal_recording.scope,
        )
        _require_matching_scope(type(self).__name__, self.scope, self.chapters.scope)

    def artifact_refs(self) -> tuple[ArtifactRef, ...]:
        return artifact_refs(self.prompt, self.terminal_recording, self.chapters)

    def to_manifest_fields(self) -> dict[str, object]:
        return {
            "issue_number": self.scope.issue_number.value,
            "exchange_run_id": self.scope.exchange_run_id.value,
            "round_index": self.scope.round_index.value,
            "attempt_index": self.scope.attempt_index.value,
            "role": self.scope.role.value,
            "provider": self.scope.provider.value,
            "artifacts": [ref.to_event_artifact() for ref in self.artifact_refs()],
        }


@dataclass(frozen=True, slots=True)
class ReviewerTurnStarted(AgentTurnStarted):
    """Reviewer turn-start contract."""

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_role(type(self).__name__, self.scope, AgentRole.REVIEWER)


@dataclass(frozen=True, slots=True)
class CoderTurnStarted(AgentTurnStarted):
    """Coder turn-start contract."""

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_role(type(self).__name__, self.scope, AgentRole.CODER)


@dataclass(frozen=True, slots=True)
class AgentTurnCompleted:
    """Required artifacts known when one agent turn completes."""

    started: AgentTurnStarted
    response: ReviewResponseArtifact
    response_type: str
    response_text: str

    def __post_init__(self) -> None:
        _require_matching_scope(
            type(self).__name__,
            self.started.scope,
            self.response.scope,
        )
        _require_non_empty_string(type(self).__name__, "response_type", self.response_type)
        _require_non_empty_string(type(self).__name__, "response_text", self.response_text)

    def artifact_refs(self) -> tuple[ArtifactRef, ...]:
        return (*self.started.artifact_refs(), self.response.to_ref())

    def to_manifest_fields(self) -> dict[str, object]:
        fields = self.started.to_manifest_fields()
        fields["response_type"] = self.response_type
        fields["response_text"] = self.response_text
        fields["artifacts"] = [ref.to_event_artifact() for ref in self.artifact_refs()]
        return fields


@dataclass(frozen=True, slots=True)
class ReviewerTurnCompleted(AgentTurnCompleted):
    """Reviewer turn-completion contract."""

    started: ReviewerTurnStarted


@dataclass(frozen=True, slots=True)
class CoderTurnCompleted(AgentTurnCompleted):
    """Coder turn-completion contract."""

    started: CoderTurnStarted


def artifact_refs(*artifacts: _FileArtifact) -> tuple[ArtifactRef, ...]:
    """Convert typed artifact wrappers to refs without filesystem discovery."""
    return tuple(artifact.to_ref() for artifact in artifacts)


def _require_matching_scope(
    contract: str,
    expected: ArtifactScope,
    actual: ArtifactScope,
) -> None:
    if actual != expected:
        raise ArtifactContractViolation(
            contract,
            "scope",
            f"artifact scope mismatch: expected {expected!r}, got {actual!r}",
        )


def _require_role(
    contract: str,
    scope: AgentTurnArtifactScope,
    role: AgentRole,
) -> None:
    if scope.role is not role:
        raise ArtifactContractViolation(
            contract,
            "scope.role",
            f"must be {role.value}",
        )


__all__ = [
    "AgentProvider",
    "AgentRole",
    "AgentTurnCompleted",
    "AgentTurnArtifactScope",
    "AgentTurnStarted",
    "ArtifactContractViolation",
    "ArtifactRef",
    "ArtifactScope",
    "ArtifactType",
    "CoderTurnCompleted",
    "CoderTurnStarted",
    "ChapterSidecarArtifact",
    "CompletionRecordArtifact",
    "ExchangeRunId",
    "ExistingDirectory",
    "ExistingFile",
    "ExistingNonEmptyFile",
    "IssueNumber",
    "PositiveAttemptIndex",
    "PositiveRoundIndex",
    "PromptArtifact",
    "RenderMode",
    "ReviewerTurnCompleted",
    "ReviewExchangeArtifactScope",
    "ReviewExchangeSummaryArtifact",
    "ReviewResponseArtifact",
    "ReviewerTurnStarted",
    "RunArtifactScope",
    "RESERVED_VALIDATION_OUTCOME_FIELDS",
    "TerminalRecordingArtifact",
    "ValidationArtifactScope",
    "ValidationFailed",
    "ValidationOutcome",
    "ValidationPassed",
    "ValidationResultArtifact",
    "ValidationRetry",
    "artifact_refs",
    "validation_outcome_from_manifest_fields",
    "validation_outcome_to_manifest_fields",
]
