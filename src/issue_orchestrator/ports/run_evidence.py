"""Ports for recording run-scoped evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from .session_output import ValidationRecord


class ValidationEvidenceRecorder(Protocol):
    """Records validation evidence for a session run."""

    def record_validation_evidence(
        self,
        *,
        run_dir: Path,
        worktree: Path,
        record: ValidationRecord | None,
        record_path: Path | None = None,
        junit_xml_paths: tuple[str, ...] | list[str] = (),
    ) -> Any:
        """Record validation artifacts and structured test reports."""
        ...


class NullValidationEvidenceRecorder:
    """No-op recorder used by tests that do not exercise evidence wiring."""

    def record_validation_evidence(
        self,
        *,
        run_dir: Path,
        worktree: Path,
        record: ValidationRecord | None,
        record_path: Path | None = None,
        junit_xml_paths: tuple[str, ...] | list[str] = (),
    ) -> None:
        del run_dir, worktree, record, record_path, junit_xml_paths
