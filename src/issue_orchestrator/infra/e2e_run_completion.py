"""E2E worker completion policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class E2ECompletionDecision:
    """Final run status, exit code, and note lines."""

    status: str
    exit_code: int
    notes: list[str]


def status_from_cases(cases: list[Any], quarantine: set[str]) -> str | None:
    """Derive a run status from parsed structured results."""
    if not cases:
        return None
    failed_cases = [case for case in cases if case.outcome in {"failed", "error"}]
    if any(case.case_id not in quarantine for case in failed_cases):
        return "failed"
    if failed_cases:
        return "warning"
    return "passed"


def run_report_artifact_dir(repo_root: Path, run_id: int) -> Path:
    """Return the worktree-local, run-scoped report artifact directory."""
    return repo_root / ".issue-orchestrator" / "e2e-results" / f"run_{run_id}"


def quarantined_failure_nodeids(db: Any, run_id: int) -> list[str]:
    """Return quarantined failures persisted for the run."""
    summary = db.get_test_summary(run_id)
    return [
        str(test.get("nodeid"))
        for test in summary.get("quarantined", [])
        if test.get("outcome") in {"failed", "error"}
        and test.get("retry_outcome") != "passed"
        and test.get("nodeid")
    ]


def decide_completion(
    *,
    db: Any,
    run_id: int,
    runner_kind: str,
    exit_code: int,
    failed_tests: list[str],
    fixture_errors: list[str],
    retried_passed: list[str],
    structured_status: str | None,
) -> E2ECompletionDecision:
    """Determine final worker status and user-visible notes."""
    notes, quarantined = _completion_notes(
        db=db,
        run_id=run_id,
        runner_kind=runner_kind,
        exit_code=exit_code,
        fixture_errors=fixture_errors,
        retried_passed=retried_passed,
        structured_status=structured_status,
    )
    return _select_completion_decision(
        runner_kind=runner_kind,
        exit_code=exit_code,
        failed_tests=failed_tests,
        fixture_errors=fixture_errors,
        retried_passed=retried_passed,
        structured_status=structured_status,
        quarantined=quarantined,
        notes=notes,
    )


def _completion_notes(
    *,
    db: Any,
    run_id: int,
    runner_kind: str,
    exit_code: int,
    fixture_errors: list[str],
    retried_passed: list[str],
    structured_status: str | None,
) -> tuple[list[str], list[str]]:
    """Build user-visible completion notes and return quarantined failures."""
    notes: list[str] = []
    if fixture_errors:
        notes.append("Fixture errors: " + "; ".join(fixture_errors[:5]))
    if retried_passed:
        short = [n.split("::")[-1] for n in retried_passed]
        notes.append(
            f"{len(retried_passed)} test(s) required retry: " + ", ".join(short)
        )

    quarantined = quarantined_failure_nodeids(db, run_id)
    if quarantined:
        short = [n.split("::")[-1] for n in quarantined[:5]]
        notes.append(
            f"{len(quarantined)} quarantined test(s) failed: " + ", ".join(short)
        )
    if runner_kind == "command" and structured_status == "failed" and exit_code == 0:
        notes.append("Command exited 0 but JUnit XML reported failing tests")
    return notes, quarantined


def _select_completion_decision(
    *,
    runner_kind: str,
    exit_code: int,
    failed_tests: list[str],
    fixture_errors: list[str],
    retried_passed: list[str],
    structured_status: str | None,
    quarantined: list[str],
    notes: list[str],
) -> E2ECompletionDecision:
    """Map worker facts to final run status and exit code."""
    if fixture_errors:
        return E2ECompletionDecision("failed", exit_code, notes)
    if retried_passed and exit_code == 0:
        return E2ECompletionDecision("warning", exit_code, notes)
    if structured_status == "failed":
        return E2ECompletionDecision("failed", exit_code, notes)
    if quarantined and not failed_tests:
        if exit_code != 0:
            notes.append(
                f"Runner exit code {exit_code} ignored because only quarantined tests failed"
            )
            exit_code = 0
        return E2ECompletionDecision("warning", exit_code, notes)
    if exit_code == 0:
        return E2ECompletionDecision("passed", exit_code, notes)
    if runner_kind == "pytest" and exit_code == 5:
        return E2ECompletionDecision("passed", exit_code, notes)
    return E2ECompletionDecision("failed", exit_code, notes)
