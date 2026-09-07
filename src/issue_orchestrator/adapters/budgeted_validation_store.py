"""Atomic, process-locked budgeted validation history shared by Git worktrees."""

from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path

from ..domain.budgeted_validation import (
    BudgetedValidationHistory, BudgetedValidationOutcome, BudgetedValidationProbe,
    BudgetedValidationRun, BudgetedValidationSuite, BudgetedValidationReportReceipt,
)
from ..ports.budgeted_validation import BudgetedValidationJournal


def suite_identity(suite: BudgetedValidationSuite) -> str:
    definition = (suite.command, suite.setup_command, suite.branch)
    return hashlib.sha256(json.dumps(definition).encode()).hexdigest()


def _decode_run(data: dict | None) -> BudgetedValidationRun | None:
    if data is None:
        return None
    probe = data["probe"]
    return BudgetedValidationRun(
        id=data["id"], started_at=datetime.fromisoformat(data["started_at"]),
        finished_at=datetime.fromisoformat(data["finished_at"]) if data["finished_at"] else None,
        probe=BudgetedValidationProbe(probe["commit"], BudgetedValidationOutcome(probe["outcome"]),
                                     probe["evidence"], probe["failure_signature"]),
        purpose=data["purpose"],
    )


class FileBudgetedValidationStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._leased = False

    def run_exclusive(self, operation: Callable[[BudgetedValidationJournal], None]) -> bool:
        self._directory.mkdir(parents=True, exist_ok=True)
        with (self._directory / "run.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            self._leased = True
            try:
                operation(self)
            finally:
                self._leased = False
                fcntl.flock(lock, fcntl.LOCK_UN)
        return True

    def _path(self, suite: BudgetedValidationSuite) -> Path:
        return self._directory / f"{suite.name}-{suite_identity(suite)}.json"

    def read(self, suite: BudgetedValidationSuite) -> BudgetedValidationHistory:
        path = self._path(suite)
        identity = suite_identity(suite)
        if not path.exists():
            return BudgetedValidationHistory(identity)
        data = json.loads(path.read_text())
        if data["version"] != 1 or data["suite_identity"] != identity:
            raise ValueError("Unrecognised budgeted validation history")
        runs = tuple(_decode_run(run) for run in data["runs"])
        if any(run is None for run in runs):
            raise ValueError("Invalid null validation run")
        return BudgetedValidationHistory(
            suite_identity=identity, last_success=_decode_run(data["last_success"]),
            latest=_decode_run(data["latest"]), last_scheduled=_decode_run(data["last_scheduled"]),
            runs=tuple(run for run in runs if run is not None),
            first_bad_commit=data["first_bad_commit"], diagnosis=data["diagnosis"],
        )

    def read_report(self, case_id: str) -> BudgetedValidationReportReceipt:
        path = self._directory / f"report-{hashlib.sha256(case_id.encode()).hexdigest()}.json"
        if not path.exists():
            return BudgetedValidationReportReceipt()
        data = json.loads(path.read_text())
        if data["version"] != 1:
            raise ValueError("Unrecognised regression report receipt")
        return BudgetedValidationReportReceipt(
            datetime.fromisoformat(data["attempted_at"]) if data["attempted_at"] else None,
            data["issue_number"],
            datetime.fromisoformat(data["next_lookup_at"]) if data["next_lookup_at"] else None,
        )

    def write_report(self, case_id: str, receipt: BudgetedValidationReportReceipt) -> None:
        if not self._leased:
            raise RuntimeError("Report writes require the repository lease")
        path = self._directory / f"report-{hashlib.sha256(case_id.encode()).hexdigest()}.json"
        self._write_json(path, {"version": 1, **asdict(receipt)})

    def write(self, suite: BudgetedValidationSuite, history: BudgetedValidationHistory) -> None:
        if not self._leased or history.suite_identity != suite_identity(suite):
            raise RuntimeError("History writes require the matching suite and repository lease")
        self._write_json(self._path(suite), {"version": 1, **asdict(history)})

    def _write_json(self, path: Path, data: dict) -> None:
        temporary = path.with_suffix(".tmp")
        with temporary.open("w") as output:
            json.dump(data, output, default=_json_value, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        directory_fd = os.open(self._directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _json_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported history value {type(value).__name__}")
