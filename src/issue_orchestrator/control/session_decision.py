"""Session outcome decision payloads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..domain.models import SessionStatus

if TYPE_CHECKING:
    from .completion_processor import ProcessingResult


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
