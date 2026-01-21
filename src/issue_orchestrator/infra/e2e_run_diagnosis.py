"""E2E run failure diagnosis for the web UI.

This module provides diagnostic info for debugging failed E2E test runs,
used by the /control/e2e/diagnosis/{run_id} endpoint.

Follows the same pattern as session_failure_diagnosis.py.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .e2e_db import E2EDB, E2ERun
from .issue_diagnostics import DiagnosticReference

logger = logging.getLogger(__name__)


@dataclass
class E2ERunDiagnosis:
    """Comprehensive diagnosis info for an E2E test run failure.

    This is returned by the API to the web layer for the
    /control/e2e/diagnosis/{run_id} endpoint.
    """

    run_id: int
    status: str
    commit_sha: str | None
    branch: str | None
    started_at: str
    finished_at: str | None
    duration_seconds: float | None
    log_path: str | None
    log_exists: bool
    log_content: str | None

    # Test summary
    total_tests: int
    passed_count: int
    failed_count: int
    passed_on_retry_count: int
    quarantined_count: int
    skipped_count: int

    # Failed test details (nodeid, longrepr, duration)
    failed_tests: list[dict] = field(default_factory=list)
    flaky_tests: list[dict] = field(default_factory=list)  # Passed on retry

    # Context
    pytest_args: list[str] = field(default_factory=list)
    repo_root: str = ""
    orchestrator_id: str = ""

    # Diagnosis helpers
    warnings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "log_path": self.log_path,
            "log_exists": self.log_exists,
            "log_content": self.log_content,
            "total_tests": self.total_tests,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "passed_on_retry_count": self.passed_on_retry_count,
            "quarantined_count": self.quarantined_count,
            "skipped_count": self.skipped_count,
            "failed_tests": self.failed_tests,
            "flaky_tests": self.flaky_tests,
            "pytest_args": self.pytest_args,
            "repo_root": self.repo_root,
            "orchestrator_id": self.orchestrator_id,
            "warnings": self.warnings,
            "suggestions": self.suggestions,
        }


def _read_log_content(log_path: str | None) -> tuple[bool, str | None]:
    """Read the full log file content.

    Returns:
        (log_exists, log_content) tuple
    """
    if not log_path:
        return False, None

    path = Path(log_path)
    if not path.exists():
        return False, None

    try:
        return True, path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("Failed to read log file %s: %s", log_path, e)
        return True, f"[Error reading log: {e}]"


def _build_warnings_and_suggestions(
    run: E2ERun,
    failed_count: int,
    flaky_count: int,
    total_tests: int,
) -> tuple[list[str], list[str]]:
    """Build warnings and suggestions based on run analysis."""
    warnings: list[str] = []
    suggestions: list[str] = []

    # High failure rate
    if total_tests > 0:
        failure_rate = failed_count / total_tests
        if failure_rate > 0.5:
            warnings.append(f"High failure rate: {failure_rate:.0%} of tests failed")
            suggestions.append("Check for environment issues or breaking changes")
        elif failure_rate > 0.2:
            warnings.append(f"Elevated failure rate: {failure_rate:.0%} of tests failed")

    # Many flaky tests
    if flaky_count > 3:
        warnings.append(f"{flaky_count} tests are flaky (passed on retry)")
        suggestions.append("Consider adding flaky tests to quarantine list")

    # Run was interrupted
    if run.status == "interrupted":
        warnings.append("Run was interrupted (worker process died)")
        suggestions.append("Check for OOM or timeout issues")

    # Run was canceled
    if run.status == "canceled":
        warnings.append("Run was canceled by user")

    # No tests ran
    if total_tests == 0:
        warnings.append("No tests were collected")
        suggestions.append("Check pytest args and test discovery")

    return warnings, suggestions


def create_e2e_run_diagnosis(run_id: int, db: E2EDB) -> E2ERunDiagnosis | None:
    """Create a comprehensive diagnosis for an E2E run.

    Args:
        run_id: The E2E run ID to diagnose
        db: E2EDB instance

    Returns:
        E2ERunDiagnosis with full diagnostic data, or None if run not found
    """
    details = db.run_details(run_id)
    if not details:
        return None

    run_dict = details["run"]
    run = E2ERun(
        id=run_dict["id"],
        repo_root=run_dict["repo_root"],
        orchestrator_id=run_dict["orchestrator_id"],
        started_at=run_dict["started_at"],
        finished_at=run_dict["finished_at"],
        status=run_dict["status"],
        exit_code=run_dict["exit_code"],
        pytest_args=run_dict["pytest_args"],
        commit_sha=run_dict["commit_sha"],
        branch=run_dict["branch"],
        retry_of=run_dict["retry_of"],
        is_retry_run=run_dict["is_retry_run"],
        duration_seconds=run_dict["duration_seconds"],
        note=run_dict["note"],
        log_path=run_dict["log_path"],
        artifacts_dir=run_dict["artifacts_dir"],
        worker_pid=run_dict["worker_pid"],
        total_tests=run_dict["total_tests"],
        current_test=run_dict["current_test"],
    )

    summary = db.get_test_summary(run_id)
    counts = summary["counts"]

    # Extract failed test details with full longrepr
    failed_tests = [
        {
            "nodeid": t["nodeid"],
            "longrepr": t["longrepr"],
            "duration_seconds": t["duration_seconds"],
        }
        for t in summary["failed"]
    ]

    # Extract flaky tests (passed on retry)
    flaky_tests = [
        {
            "nodeid": t["nodeid"],
            "longrepr": t["longrepr"],
            "duration_seconds": t["duration_seconds"],
        }
        for t in summary["passed_on_retry"]
    ]

    # Read full log content
    log_exists, log_content = _read_log_content(run.log_path)

    # Build warnings and suggestions
    warnings, suggestions = _build_warnings_and_suggestions(
        run,
        counts["failed"],
        counts["passed_on_retry"],
        counts["total"],
    )

    return E2ERunDiagnosis(
        run_id=run.id,
        status=run.status,
        commit_sha=run.commit_sha,
        branch=run.branch,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=run.duration_seconds,
        log_path=run.log_path,
        log_exists=log_exists,
        log_content=log_content,
        total_tests=counts["total"],
        passed_count=counts["passed"],
        failed_count=counts["failed"],
        passed_on_retry_count=counts["passed_on_retry"],
        quarantined_count=counts["quarantined"],
        skipped_count=counts["skipped"],
        failed_tests=failed_tests,
        flaky_tests=flaky_tests,
        pytest_args=run.pytest_args,
        repo_root=run.repo_root,
        orchestrator_id=run.orchestrator_id,
        warnings=warnings,
        suggestions=suggestions,
    )


def write_e2e_diagnostic(
    repo_root: Path,
    diagnosis: E2ERunDiagnosis,
) -> DiagnosticReference | None:
    """Write a diagnostic JSON file for an E2E run.

    Follows the pattern from issue_diagnostics.py.

    Args:
        repo_root: Repository root path
        diagnosis: E2ERunDiagnosis to write

    Returns:
        DiagnosticReference for use in issue body, or None on failure
    """
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    diagnostics_dir = repo_root / ".issue-orchestrator" / "diagnostics"
    filename = f"{timestamp}-e2e-run-{diagnosis.run_id}.json"
    path = diagnostics_dir / filename

    payload = {
        "type": "e2e_run_diagnosis",
        "run_id": diagnosis.run_id,
        "timestamp": timestamp,
        "diagnosis": diagnosis.to_dict(),
    }

    try:
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        relative_path = str(path.relative_to(repo_root))
        # Use repo name as worktree_name for E2E (runs from main repo)
        return DiagnosticReference(
            worktree_name=repo_root.name,
            relative_path=relative_path,
        )
    except Exception as exc:
        logger.warning("Failed to write E2E diagnostic file: %s", exc)
        return None


def generate_diagnostic_issue_body(
    diagnosis: E2ERunDiagnosis,
    diagnostic_ref: DiagnosticReference | None,
) -> str:
    """Generate a markdown issue body for E2E failure diagnosis.

    The body contains a summary with a reference to the full diagnostic file.

    Args:
        diagnosis: E2ERunDiagnosis with failure details
        diagnostic_ref: Reference to the diagnostic file

    Returns:
        Markdown-formatted issue body
    """
    lines = [
        "## E2E Test Run Failure",
        "",
        f"**Run ID**: {diagnosis.run_id}",
        f"**Status**: {diagnosis.status}",
        f"**Commit**: {diagnosis.commit_sha or 'unknown'}",
        f"**Branch**: {diagnosis.branch or 'unknown'}",
        f"**Duration**: {diagnosis.duration_seconds:.1f}s" if diagnosis.duration_seconds else "**Duration**: unknown",
        "",
        "## Test Results",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Total | {diagnosis.total_tests} |",
        f"| Passed | {diagnosis.passed_count} |",
        f"| Failed | {diagnosis.failed_count} |",
        f"| Flaky (passed on retry) | {diagnosis.passed_on_retry_count} |",
        f"| Quarantined | {diagnosis.quarantined_count} |",
        f"| Skipped | {diagnosis.skipped_count} |",
        "",
    ]

    # Failed tests summary
    if diagnosis.failed_tests:
        lines.append("## Failed Tests")
        lines.append("")
        for test in diagnosis.failed_tests[:10]:  # Limit to first 10 in issue
            lines.append(f"- `{test['nodeid']}`")
        if len(diagnosis.failed_tests) > 10:
            lines.append(f"- ... and {len(diagnosis.failed_tests) - 10} more")
        lines.append("")

    # Warnings
    if diagnosis.warnings:
        lines.append("## Warnings")
        lines.append("")
        for warning in diagnosis.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    # Suggestions
    if diagnosis.suggestions:
        lines.append("## Suggestions")
        lines.append("")
        for suggestion in diagnosis.suggestions:
            lines.append(f"- {suggestion}")
        lines.append("")

    # Diagnostic file reference
    if diagnostic_ref:
        lines.append("## Full Diagnostic Data")
        lines.append("")
        lines.append(f"Complete diagnostic information including full stack traces and logs:")
        lines.append(f"`{diagnostic_ref.relative_path}`")
        lines.append("")

    lines.append("---")
    lines.append("*Generated by issue-orchestrator E2E diagnosis*")

    return "\n".join(lines)
