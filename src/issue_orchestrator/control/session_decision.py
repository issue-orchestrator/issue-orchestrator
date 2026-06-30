"""Session outcome decision payloads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..domain.models import SessionStatus

if TYPE_CHECKING:
    from .completion_processor import ProcessingResult
    from ..infra.provider_resilience import ProviderStatus


@dataclass(frozen=True)
class ProviderTransientFailureDecision:
    """Provider-circuit failure effect to apply on the tick thread."""

    provider: str | None
    error_summary: str | None
    attempts: int | None


def provider_success_from_status(status: "ProviderStatus | None") -> str | None:
    if status and status.succeeded:
        return status.provider
    return None


def provider_failure_from_status(
    status: "ProviderStatus",
) -> ProviderTransientFailureDecision:
    return ProviderTransientFailureDecision(
        provider=status.provider,
        error_summary=status.last_error_summary,
        attempts=status.attempts,
    )


@dataclass
class SessionDecision:
    """Decision about a session's outcome."""

    status: SessionStatus
    processing_result: "ProcessingResult | None" = None
    completion_processed: bool = False
    recovered_from_timeout: bool = False
    reason: str = ""
    validation_passed: bool | None = None
    validation_error: str | None = None
    validation_error_file: Path | None = None
    blocked_label: str | None = None
    blocked_reason: str | None = None
    completion_detail: dict[str, Any] | None = None
    diagnostic_path: str | None = None
    provider_success: str | None = None
    provider_transient_failure: ProviderTransientFailureDecision | None = None
