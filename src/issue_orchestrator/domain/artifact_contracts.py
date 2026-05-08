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
    """Stable review-exchange run identity, not a filesystem path."""

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
    role: AgentRole
    provider: AgentProvider


@dataclass(frozen=True, slots=True)
class ValidationArtifactScope:
    """Artifact scoped to validation evidence for one run."""

    issue_number: IssueNumber
    run_id: ExchangeRunId
    run_dir: ExistingDirectory


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
    file: ExistingNonEmptyFile


@dataclass(frozen=True, slots=True)
class ChapterSidecarArtifact(_FileArtifact):
    artifact_type: ClassVar[ArtifactType] = ArtifactType.CHAPTER_SIDECAR
    label: ClassVar[str] = "Replay Chapters"
    render_mode: ClassVar[RenderMode] = RenderMode.JSON


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


def artifact_refs(*artifacts: _FileArtifact) -> tuple[ArtifactRef, ...]:
    """Convert typed artifact wrappers to refs without filesystem discovery."""
    return tuple(artifact.to_ref() for artifact in artifacts)


__all__ = [
    "AgentProvider",
    "AgentRole",
    "AgentTurnArtifactScope",
    "ArtifactContractViolation",
    "ArtifactRef",
    "ArtifactScope",
    "ArtifactType",
    "ChapterSidecarArtifact",
    "CompletionRecordArtifact",
    "ExchangeRunId",
    "ExistingDirectory",
    "ExistingFile",
    "ExistingNonEmptyFile",
    "IssueNumber",
    "PositiveRoundIndex",
    "PromptArtifact",
    "RenderMode",
    "ReviewExchangeArtifactScope",
    "ReviewExchangeSummaryArtifact",
    "ReviewResponseArtifact",
    "RunArtifactScope",
    "TerminalRecordingArtifact",
    "ValidationArtifactScope",
    "ValidationResultArtifact",
    "artifact_refs",
]
