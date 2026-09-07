"""Immutable completion custody and run-bound submission contracts."""

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from .historical_intake import HistoricalIntakeCommand
from .session_run import SessionRunAssets, SessionRunIdentity
from .validated_work import require_sha, require_text


class CompletionIntakeError(RuntimeError):
    """Intake could not establish durable, authentic custody."""


class IntakeUnauthorized(CompletionIntakeError):
    """No allocated run matches the submission capability."""


class IntakeClosed(CompletionIntakeError):
    """The run owner has frozen its submission set."""


class SubmissionConflict(CompletionIntakeError):
    """A retry key already names different bytes."""


class CompletionValidationFailed(CompletionIntakeError):
    """Trusted failed validation is a retryable outcome, not damaged custody."""

    def __init__(self, result_path: Path) -> None:
        super().__init__("configured completion validation failed")
        self.result_path = result_path


class CompletionParseStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class SubmissionOrigin(StrEnum):
    RUN_CAPABILITY = "run_capability"
    HISTORICAL_OPERATOR = "historical_operator"


@dataclass(frozen=True, slots=True)
class SubmitCompletionEvidence:
    raw_bytes: bytes = field(repr=False)
    content_sha256: str
    submission_key: str

    def __post_init__(self) -> None:
        require_sha(self.content_sha256, size=64)
        require_text(self.submission_key, "submission_key")
        if len(self.submission_key) > 128:
            raise ValueError("submission key exceeds 128 characters")
        if type(self.raw_bytes) is not bytes or len(self.raw_bytes) > 2 * 1024 * 1024:
            raise ValueError("completion must be at most 2 MiB of bytes")
        if sha256(self.raw_bytes).hexdigest() != self.content_sha256:
            raise ValueError("completion content hash mismatch")


@dataclass(frozen=True, slots=True)
class OwnedCompletionSubmission:
    command: SubmitCompletionEvidence
    origin: SubmissionOrigin
    actor: str
    historical_command: HistoricalIntakeCommand | None = None

    def __post_init__(self) -> None:
        if type(self.origin) is not SubmissionOrigin:
            raise ValueError("submission provenance must be typed")
        require_text(self.actor, "actor")
        if (self.origin is SubmissionOrigin.HISTORICAL_OPERATOR) != (
            self.historical_command is not None
        ):
            raise ValueError(
                "historical provenance requires the exact operator command"
            )


@dataclass(frozen=True, slots=True)
class CompletionIntakeReceipt:
    entry_id: str
    content_sha256: str

    def __post_init__(self) -> None:
        require_sha(self.entry_id, size=64)
        require_sha(self.content_sha256, size=64)


@dataclass(frozen=True, slots=True)
class CompletionIntakeEntry:
    entry_id: str
    run: SessionRunAssets
    submission_key: str
    receive_sequence: int
    raw_sha256: str
    byte_size: int
    raw_path: Path
    parse_status: CompletionParseStatus
    normalization_version: int | None
    normalized_sha256: str | None
    normalized_path: Path | None
    origin: SubmissionOrigin
    actor: str
    received_at: str

    def __post_init__(self) -> None:
        require_sha(self.entry_id, size=64)
        require_sha(self.raw_sha256, size=64)
        require_text(self.submission_key, "submission_key")
        if (
            type(self.receive_sequence) is not int
            or self.receive_sequence <= 0
            or type(self.byte_size) is not int
            or not 0 <= self.byte_size <= 2 * 1024 * 1024
        ):
            raise ValueError("invalid intake sequence or byte size")
        if (
            type(self.parse_status) is not CompletionParseStatus
            or type(self.origin) is not SubmissionOrigin
        ):
            raise ValueError("intake status and origin must be typed")
        if self.parse_status is CompletionParseStatus.ACCEPTED:
            if (
                self.normalization_version != 1
                or self.normalized_path is None
                or self.normalized_sha256 is None
            ):
                raise ValueError("accepted completion requires versioned normalization")
            require_sha(self.normalized_sha256, size=64)
        elif any(
            value is not None
            for value in (
                self.normalization_version,
                self.normalized_path,
                self.normalized_sha256,
            )
        ):
            raise ValueError("rejected bytes cannot have normalized authority")

    @property
    def receipt(self) -> CompletionIntakeReceipt:
        return CompletionIntakeReceipt(self.entry_id, self.raw_sha256)


@dataclass(frozen=True, slots=True)
class ValidationBinding:
    entry_id: str
    run: SessionRunIdentity
    raw_sha256: str
    normalized_sha256: str

    def __post_init__(self) -> None:
        for digest in (self.entry_id, self.raw_sha256, self.normalized_sha256):
            require_sha(digest, size=64)


@dataclass(frozen=True, slots=True)
class OwnedValidationResult:
    """Produced from a fresh ValidationRunner result, never deserialized input."""

    binding: ValidationBinding
    head_sha: str
    validator_digest: str
    result_bytes: bytes = field(repr=False)
    stdout_bytes: bytes = field(repr=False)
    stderr_bytes: bytes = field(repr=False)
    passed: bool
    recorded_at: str

    def __post_init__(self) -> None:
        require_sha(self.head_sha)
        require_sha(self.validator_digest, size=64)
        if type(self.passed) is not bool:
            raise ValueError("validation outcome must be boolean")


@dataclass(frozen=True, slots=True)
class CompletionValidationAttestation:
    binding: ValidationBinding
    head_sha: str
    validator_digest: str
    result_sha256: str
    result_path: Path
    passed: bool
    recorded_at: str
