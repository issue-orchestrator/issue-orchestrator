"""Run-scoped evidence catalog.

The run manifest is the contract for operator-facing evidence. Producers record
artifacts here once; UI routes and diagnostics consume the recorded contract
instead of re-discovering files from configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..domain.run_manifest import RunManifest
from ..infra.e2e_reports import discover_report_artifacts
from ..ports.session_output import SessionOutput, ValidationRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordedRunEvidence:
    """Paths recorded for one run evidence update."""

    validation_record_path: str | None = None
    validation_stdout_path: str | None = None
    validation_stderr_path: str | None = None
    junit_xml_paths: tuple[str, ...] = ()


class RunEvidenceRecorder:
    """Owner for writing run evidence into ``manifest.json``."""

    def __init__(self, session_output: SessionOutput) -> None:
        self._session_output = session_output

    def record_validation_evidence(
        self,
        *,
        run_dir: Path,
        worktree: Path,
        record: ValidationRecord | None,
        record_path: Path | None = None,
        junit_xml_paths: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Record validation logs and structured test artifacts for a run."""
        evidence = _validation_evidence(
            worktree=worktree,
            record=record,
            record_path=record_path,
            junit_xml_paths=junit_xml_paths,
        )
        updates: dict[str, Any] = {}
        if evidence.validation_record_path:
            updates["validation_record_path"] = evidence.validation_record_path
        if evidence.validation_stdout_path:
            updates["validation_stdout"] = evidence.validation_stdout_path
        if evidence.validation_stderr_path:
            updates["validation_stderr"] = evidence.validation_stderr_path
        artifacts = _merged_artifacts(
            self._session_output.read_manifest(run_dir) or {},
            junit_xml_paths=evidence.junit_xml_paths,
        )
        if artifacts:
            updates["artifacts"] = artifacts
        if updates:
            self._session_output.update_manifest(run_dir, updates)


def recorded_junit_xml_paths(run_dir: Path) -> tuple[str, ...]:
    """Return JUnit XML paths recorded in a run manifest."""
    try:
        manifest = RunManifest.load(run_dir)
    except FileNotFoundError:
        return ()
    return manifest.junit_xml_paths()


def recorded_validation_junit_xml_paths(run_dir: Path) -> tuple[str, ...]:
    """Return validation JUnit XML paths recorded in a run manifest."""
    try:
        manifest = RunManifest.load(run_dir)
    except FileNotFoundError:
        return ()
    return manifest.junit_xml_paths(key_prefix="validation_junit_xml_")


def _validation_evidence(
    *,
    worktree: Path,
    record: ValidationRecord | None,
    record_path: Path | None,
    junit_xml_paths: tuple[str, ...] | list[str],
) -> RecordedRunEvidence:
    resolved_record_path = (
        _resolve_record_path(worktree, str(record_path)) if record_path else None
    )
    stdout_path = _resolve_record_path(worktree, record.stdout_path) if record else None
    stderr_path = _resolve_record_path(worktree, record.stderr_path) if record else None
    return RecordedRunEvidence(
        validation_record_path=(
            str(resolved_record_path)
            if resolved_record_path and resolved_record_path.exists()
            else None
        ),
        validation_stdout_path=str(stdout_path) if stdout_path and stdout_path.exists() else None,
        validation_stderr_path=str(stderr_path) if stderr_path and stderr_path.exists() else None,
        junit_xml_paths=_discover_junit_paths(worktree, junit_xml_paths),
    )


def _resolve_record_path(worktree: Path, value: str | None) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    return candidate if candidate.is_absolute() else worktree / candidate


def _discover_junit_paths(
    worktree: Path,
    junit_xml_paths: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    paths = tuple(path for path in junit_xml_paths if path)
    if not paths:
        return ()
    try:
        _, artifacts = discover_report_artifacts(
            worktree,
            junit_xml_paths=paths,
            artifact_paths=(),
        )
    except ValueError as exc:
        logger.debug("No validation JUnit evidence recorded under %s: %s", worktree, exc)
        return ()
    return tuple(artifact.path for artifact in artifacts if artifact.kind == "junit_xml")


def _merged_artifacts(
    manifest: dict[str, Any],
    *,
    junit_xml_paths: tuple[str, ...],
) -> dict[str, Any]:
    artifacts_raw = manifest.get("artifacts")
    artifacts = dict(artifacts_raw) if isinstance(artifacts_raw, dict) else {}
    artifacts = {
        key: value
        for key, value in artifacts.items()
        if not (
            isinstance(value, dict)
            and value.get("kind") == "junit_xml"
            and str(key).startswith("validation_junit_xml_")
        )
    }
    for path in sorted(junit_xml_paths):
        artifacts[f"validation_junit_xml_{_artifact_key_suffix(path)}"] = {
            "kind": "junit_xml",
            "path": path,
            "content_type": "application/xml",
        }
    return artifacts


def _artifact_key_suffix(path: str) -> str:
    return sha256(path.encode("utf-8")).hexdigest()[:12]
