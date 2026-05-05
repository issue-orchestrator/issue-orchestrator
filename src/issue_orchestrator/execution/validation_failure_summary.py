"""Helpers for summarizing failed validation runs for operator-facing UI."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain.run_manifest import RunManifest
from ..infra.e2e_reports import JUnitCaseResult, discover_report_artifacts
from ..ports.session_output import ValidationRecord

logger = logging.getLogger(__name__)

_FAILED_TEST_RE = re.compile(r"^FAILED\s+(\S+)")
_MAX_FAILED_TESTS = 10
_MAX_STDOUT_EXCERPT_LINES = 40
_MAX_STDERR_EXCERPT_LINES = 20
_FAILURES_MARKER = "=================================== FAILURES ==================================="
_FAILURES_END_MARKERS = (
    "============================= slowest",
    "=========================== short test summary info ============================",
)


@dataclass(frozen=True)
class ValidationFailureSummary:
    reason: str
    suite: str
    command: str
    exit_code: int
    started_at: str
    ended_at: str
    failed_tests: tuple[str, ...]
    stdout_excerpt: tuple[str, ...]
    stderr_excerpt: tuple[str, ...]
    validation_record_path: str | None
    validation_stdout_path: str | None
    validation_stderr_path: str | None
    # Parsed JUnit cases (empty when validation didn't emit JUnit XML or no
    # paths configured). When non-empty, the dashboard renders a structured
    # test-results view scoped to this validation run.
    junit_cases: tuple[JUnitCaseResult, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "suite": self.suite,
            "command": self.command,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "failed_tests": list(self.failed_tests),
            "stdout_excerpt": list(self.stdout_excerpt),
            "stderr_excerpt": list(self.stderr_excerpt),
            "validation_record_path": self.validation_record_path,
            "validation_stdout_path": self.validation_stdout_path,
            "validation_stderr_path": self.validation_stderr_path,
            # Structured per-test results when JUnit XML is configured.
            # The dashboard renders these as a per-test table with
            # actual failure details — the actionable info users need
            # to figure out what went wrong without scrolling stdout.
            "junit_cases": [_junit_case_to_dict(case) for case in self.junit_cases],
        }


def _junit_case_to_dict(case: JUnitCaseResult) -> dict[str, Any]:
    """Serialize a `JUnitCaseResult` to the dict shape consumed by the
    `ValidationFailureDialogPayload.junit_cases` field. Mirrors the
    `JUnitCasePayload` schema in `docs/api/ui-openapi.json` exactly.
    """
    return {
        "case_id": case.case_id,
        "display_name": case.display_name,
        "suite_name": case.suite_name,
        "outcome": case.outcome,
        "duration_seconds": case.duration_seconds,
        "failure_details": case.failure_details,
        "system_out": case.system_out,
        "system_err": case.system_err,
    }


def load_validation_failure_summary(
    run_dir: Path,
    *,
    junit_xml_paths: tuple[str, ...] | list[str] = (),
    junit_search_root: Path | None = None,
) -> ValidationFailureSummary | None:
    """Return a concise summary when a run's validation failed.

    When ``junit_xml_paths`` is non-empty, also parse JUnit XML rooted at
    ``junit_search_root`` (defaults to the manifest's worktree, or run_dir as
    a last resort) and attach the structured cases.
    """
    try:
        manifest = RunManifest.load(run_dir)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None

    if manifest.validation_status != "failed":
        return None

    worktree = manifest.worktree if manifest.worktree else None
    record_path = _resolve_run_artifact(
        run_dir,
        manifest.validation_record_path,
        fallback_name="validation-record.json",
        worktree=worktree,
    )
    stdout_path = _resolve_run_artifact(
        run_dir,
        manifest.validation_stdout,
        fallback_name="validation-stdout.log",
        worktree=worktree,
    )
    stderr_path = _resolve_run_artifact(
        run_dir,
        manifest.validation_stderr,
        fallback_name="validation-stderr.log",
        worktree=worktree,
    )

    record = _load_validation_record(record_path) if record_path else None
    stdout_lines = _read_lines(stdout_path)
    stderr_lines = _read_lines(stderr_path)
    failed_tests = _extract_failed_tests(stdout_lines)

    junit_cases = _load_junit_cases(
        junit_xml_paths,
        junit_search_root or (Path(worktree) if worktree else run_dir),
    )

    return ValidationFailureSummary(
        reason=manifest.validation_reason or "Validation failed",
        suite=record.suite if record else "",
        command=record.command if record else "",
        exit_code=record.exit_code if record else 0,
        started_at=record.started_at if record else "",
        ended_at=record.ended_at if record else "",
        failed_tests=failed_tests,
        stdout_excerpt=_extract_stdout_excerpt(stdout_lines),
        stderr_excerpt=_extract_stderr_excerpt(stderr_lines),
        validation_record_path=str(record_path) if record_path else None,
        validation_stdout_path=str(stdout_path) if stdout_path else None,
        validation_stderr_path=str(stderr_path) if stderr_path else None,
        junit_cases=junit_cases,
    )


def load_validation_failure_summary_with_config(
    run_dir: Path,
    *,
    config: Any,
) -> ValidationFailureSummary | None:
    """Config-aware wrapper that threads ``validation.junit_xml_paths``.

    Both the dashboard's ``/api/dialog/validation-failure/`` route and
    the issue-detail diagnostic path call this helper so they cannot
    disagree on whether structured JUnit cases reach the user. If
    ``config`` is None or has no ``validation`` block, JUnit parsing is
    skipped (matches the bare ``load_validation_failure_summary``
    behavior).
    """
    junit_paths: tuple[str, ...] = ()
    if config is not None:
        validation_cfg = getattr(config, "validation", None)
        if validation_cfg is not None:
            junit_paths = tuple(getattr(validation_cfg, "junit_xml_paths", ()) or ())
    return load_validation_failure_summary(run_dir, junit_xml_paths=junit_paths)


def _load_junit_cases(
    junit_xml_paths: tuple[str, ...] | list[str],
    search_root: Path,
) -> tuple[JUnitCaseResult, ...]:
    paths = tuple(p for p in junit_xml_paths if p)
    if not paths or not search_root.exists():
        return ()
    try:
        cases, _ = discover_report_artifacts(
            search_root,
            junit_xml_paths=paths,
            artifact_paths=(),
        )
    except ValueError as exc:
        # Validation may legitimately fail before producing JUnit XML
        # (e.g., a typecheck step exits before the test step writes its
        # report). Treat that as "no structured results", not a fatal error.
        logger.debug(
            "JUnit XML not available for validation summary at %s: %s",
            search_root, exc,
        )
        return ()
    return tuple(cases)


def _resolve_run_artifact(
    run_dir: Path,
    manifest_path: str | None,
    *,
    fallback_name: str,
    worktree: str | None,
) -> Path | None:
    local_path = run_dir / fallback_name
    if local_path.exists():
        return local_path
    if not manifest_path:
        return None
    candidate = Path(manifest_path)
    if candidate.is_absolute():
        return candidate if candidate.exists() else None
    if worktree:
        worktree_candidate = Path(worktree) / candidate
        if worktree_candidate.exists():
            return worktree_candidate
    run_candidate = run_dir / candidate
    return run_candidate if run_candidate.exists() else None


def _load_validation_record(path: Path) -> ValidationRecord | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return ValidationRecord.from_dict(data)
    except (KeyError, TypeError):
        return None


def _read_lines(path: Path | None) -> list[str]:
    if path is None:
        return []
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def _extract_failed_tests(lines: list[str]) -> tuple[str, ...]:
    failed_tests: list[str] = []
    seen: set[str] = set()
    for line in lines:
        match = _FAILED_TEST_RE.match(line.strip())
        if not match:
            continue
        nodeid = match.group(1)
        if nodeid in seen:
            continue
        seen.add(nodeid)
        failed_tests.append(nodeid)
        if len(failed_tests) >= _MAX_FAILED_TESTS:
            break
    return tuple(failed_tests)


def _extract_stdout_excerpt(lines: list[str]) -> tuple[str, ...]:
    if not lines:
        return ()
    start_index = next((idx for idx, line in enumerate(lines) if _FAILURES_MARKER in line), -1)
    if start_index >= 0:
        excerpt: list[str] = []
        for line in lines[start_index:]:
            if excerpt and any(marker in line for marker in _FAILURES_END_MARKERS):
                break
            excerpt.append(line)
            if len(excerpt) >= _MAX_STDOUT_EXCERPT_LINES:
                break
        return tuple(excerpt)
    tail = [line for line in lines if line.strip()][-_MAX_STDOUT_EXCERPT_LINES:]
    return tuple(tail)


def _extract_stderr_excerpt(lines: list[str]) -> tuple[str, ...]:
    if not lines:
        return ()
    tail = [line for line in lines if line.strip()][-_MAX_STDERR_EXCERPT_LINES:]
    return tuple(tail)
