"""Row-backed data models for E2E run persistence."""

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class E2ERun:
    """A single E2E test run."""

    id: int
    repo_root: str
    orchestrator_id: str
    started_at: str
    finished_at: Optional[str]
    status: str
    exit_code: Optional[int]
    pytest_args: list[str]
    commit_sha: Optional[str]
    branch: Optional[str]
    retry_of: Optional[int]
    is_retry_run: bool
    duration_seconds: Optional[float]
    note: Optional[str]
    log_path: Optional[str]
    artifacts_dir: Optional[str]
    worker_pid: Optional[int]
    total_tests: Optional[int]
    current_test: Optional[str]
    command: list[str] = field(default_factory=list)
    runner_kind: str = "pytest"
    orchestrator_instance_id: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "E2ERun":
        return cls(
            id=row["id"],
            repo_root=row["repo_root"],
            orchestrator_id=row["orchestrator_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            exit_code=row["exit_code"],
            pytest_args=json.loads(row["pytest_args"]),
            commit_sha=row["commit_sha"],
            branch=row["branch"],
            retry_of=row["retry_of"],
            is_retry_run=bool(row["is_retry_run"]),
            duration_seconds=row["duration_seconds"],
            note=row["note"],
            log_path=row["log_path"],
            artifacts_dir=row["artifacts_dir"],
            worker_pid=row["worker_pid"],
            total_tests=row["total_tests"],
            current_test=row["current_test"],
            command=json.loads(row["command_json"]) if "command_json" in row.keys() else [],
            runner_kind=row["runner_kind"] if "runner_kind" in row.keys() else "pytest",
            orchestrator_instance_id=row["orchestrator_instance_id"] if "orchestrator_instance_id" in row.keys() else "",
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "repo_root": self.repo_root,
            "orchestrator_id": self.orchestrator_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "exit_code": self.exit_code,
            "pytest_args": self.pytest_args,
            "command": self.command,
            "runner_kind": self.runner_kind,
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "retry_of": self.retry_of,
            "is_retry_run": self.is_retry_run,
            "duration_seconds": self.duration_seconds,
            "note": self.note,
            "log_path": self.log_path,
            "artifacts_dir": self.artifacts_dir,
            "worker_pid": self.worker_pid,
            "total_tests": self.total_tests,
            "current_test": self.current_test,
            "orchestrator_instance_id": self.orchestrator_instance_id,
        }


@dataclass
class E2ETestResult:
    """A single test result within a run."""

    id: int
    run_id: int
    nodeid: str
    display_name: Optional[str]
    suite_name: Optional[str]
    result_source: str
    stdout_available: bool
    stderr_available: bool
    outcome: str
    duration_seconds: Optional[float]
    longrepr: Optional[str]
    retry_outcome: Optional[str]
    is_quarantined: bool
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "E2ETestResult":
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            nodeid=row["nodeid"],
            display_name=row["display_name"] if "display_name" in row.keys() else None,
            suite_name=row["suite_name"] if "suite_name" in row.keys() else None,
            result_source=row["result_source"] if "result_source" in row.keys() else "runtime",
            stdout_available=bool(row["stdout_available"]) if "stdout_available" in row.keys() else False,
            stderr_available=bool(row["stderr_available"]) if "stderr_available" in row.keys() else False,
            outcome=row["outcome"],
            duration_seconds=row["duration_seconds"],
            longrepr=row["longrepr"],
            retry_outcome=row["retry_outcome"],
            is_quarantined=bool(row["is_quarantined"]),
            updated_at=row["updated_at"],
        )

    @property
    def case_id(self) -> str:
        return self.nodeid

    @property
    def label(self) -> str:
        if self.display_name:
            return self.display_name
        if "::" in self.nodeid:
            return self.nodeid.split("::")[-1]
        return self.nodeid

    @property
    def failure_summary(self) -> Optional[str]:
        if not self.longrepr:
            return None
        return self.longrepr.splitlines()[0][:240]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "nodeid": self.nodeid,
            "case_id": self.case_id,
            "display_name": self.display_name,
            "label": self.label,
            "suite_name": self.suite_name,
            "result_source": self.result_source,
            "stdout_available": self.stdout_available,
            "stderr_available": self.stderr_available,
            "outcome": self.outcome,
            "duration_seconds": self.duration_seconds,
            "longrepr": self.longrepr,
            "failure_summary": self.failure_summary,
            "retry_outcome": self.retry_outcome,
            "is_quarantined": self.is_quarantined,
            "updated_at": self.updated_at,
        }


@dataclass
class E2ERunArtifact:
    """One run-scoped artifact."""

    id: int
    run_id: int
    kind: str
    label: str
    path: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "E2ERunArtifact":
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            kind=row["kind"],
            label=row["label"],
            path=row["path"],
            created_at=row["created_at"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "kind": self.kind,
            "label": self.label,
            "path": self.path,
            "created_at": self.created_at,
        }


@dataclass
class E2EFailureIssue:
    """Links a test failure to a GitHub sub-issue."""

    id: int
    nodeid: str
    github_issue_number: int
    parent_issue_number: int
    first_failing_run_id: int
    first_failing_sha: str
    last_passing_sha: Optional[str]
    resolved_at: Optional[str]
    resolution: Optional[str]  # 'passed', 'quarantined', 'manual'
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "E2EFailureIssue":
        return cls(
            id=row["id"],
            nodeid=row["nodeid"],
            github_issue_number=row["github_issue_number"],
            parent_issue_number=row["parent_issue_number"],
            first_failing_run_id=row["first_failing_run_id"],
            first_failing_sha=row["first_failing_sha"],
            last_passing_sha=row["last_passing_sha"],
            resolved_at=row["resolved_at"],
            resolution=row["resolution"],
            created_at=row["created_at"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "nodeid": self.nodeid,
            "github_issue_number": self.github_issue_number,
            "parent_issue_number": self.parent_issue_number,
            "first_failing_run_id": self.first_failing_run_id,
            "first_failing_sha": self.first_failing_sha,
            "last_passing_sha": self.last_passing_sha,
            "resolved_at": self.resolved_at,
            "resolution": self.resolution,
            "created_at": self.created_at,
        }


@dataclass
class E2ERunIssue:
    """Links an E2E run to its parent GitHub issue."""

    id: int
    run_id: int
    github_issue_number: int
    created_at: str
    closed_at: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "E2ERunIssue":
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            github_issue_number=row["github_issue_number"],
            created_at=row["created_at"],
            closed_at=row["closed_at"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "github_issue_number": self.github_issue_number,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
        }
