"""Row-backed data models for E2E run persistence."""

import json
import sqlite3
from dataclasses import dataclass
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
            outcome=row["outcome"],
            duration_seconds=row["duration_seconds"],
            longrepr=row["longrepr"],
            retry_outcome=row["retry_outcome"],
            is_quarantined=bool(row["is_quarantined"]),
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "nodeid": self.nodeid,
            "outcome": self.outcome,
            "duration_seconds": self.duration_seconds,
            "longrepr": self.longrepr,
            "retry_outcome": self.retry_outcome,
            "is_quarantined": self.is_quarantined,
            "updated_at": self.updated_at,
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
