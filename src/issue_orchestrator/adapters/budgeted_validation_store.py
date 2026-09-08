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
    BudgetedValidationRegression,
    PendingBudgetedValidation, ValidationCadence,
)
from ..ports.budgeted_validation import BudgetedValidationJournal


def suite_identity(suite: BudgetedValidationSuite) -> str:
    definition = (
        suite.command, suite.setup_command, suite.branch,
        suite.timeout_seconds, suite.setup_timeout_seconds,
    )
    return hashlib.sha256(json.dumps(definition).encode()).hexdigest()


def _decode_suite(data: dict) -> BudgetedValidationSuite:
    cadence = data["cadence"]
    return BudgetedValidationSuite(
        name=data["name"], command=tuple(data["command"]),
        setup_command=tuple(data["setup_command"]),
        cadence=ValidationCadence(**cadence),
        timeout_seconds=data["timeout_seconds"],
        setup_timeout_seconds=data["setup_timeout_seconds"],
        branch=data["branch"], enabled=data["enabled"],
        issue_agent_label=data["issue_agent_label"],
    )


def _decode_run(data: dict | None, legacy_suite: BudgetedValidationSuite | None = None) -> BudgetedValidationRun | None:
    if data is None:
        return None
    probe = data["probe"]
    return BudgetedValidationRun(
        id=data["id"], started_at=datetime.fromisoformat(data["started_at"]),
        finished_at=datetime.fromisoformat(data["finished_at"]) if data["finished_at"] else None,
        probe=BudgetedValidationProbe(probe["commit"], BudgetedValidationOutcome(probe["outcome"]),
                                     probe["evidence"], probe["failure_signature"]),
        purpose=data["purpose"],
        suite=_decode_suite(data["suite"]) if "suite" in data else _require_legacy_suite(legacy_suite),
    )


def _require_legacy_suite(suite: BudgetedValidationSuite | None) -> BudgetedValidationSuite:
    if suite is None:
        raise ValueError("legacy pending validation lacks its suite definition")
    return suite


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

    def pending(self) -> tuple[PendingBudgetedValidation, ...]:
        pending: list[PendingBudgetedValidation] = []
        for path in sorted(self._directory.glob("*.json")):
            if path.name.startswith("report-"):
                continue
            data = json.loads(path.read_text())
            if data.get("version") in {1, 2}:
                latest = data.get("latest")
                if latest is not None and latest.get("finished_at") is None:
                    raise ValueError(
                        "legacy unfinished validation lacks an exact suite definition; "
                        "reservation retained and new work refused"
                    )
                continue
            if data.get("version") != 3:
                raise ValueError(f"unrecognised validation history: {path.name}")
            history = self._decode_history(data, None)
            latest = history.latest
            if latest is not None and latest.finished_at is None:
                pending.append(PendingBudgetedValidation(latest.suite, history))
        names = [item.suite.name for item in pending]
        if len(names) != len(set(names)):
            raise ValueError("multiple unfinished definitions share one suite name")
        return tuple(pending)

    def read(self, suite: BudgetedValidationSuite) -> BudgetedValidationHistory:
        path = self._path(suite)
        identity = suite_identity(suite)
        if not path.exists():
            return BudgetedValidationHistory(identity)
        data = json.loads(path.read_text())
        if data["version"] not in {1, 2, 3} or data["suite_identity"] != identity:
            raise ValueError("Unrecognised budgeted validation history")
        return self._decode_history(data, suite)

    @staticmethod
    def _decode_history(data: dict, legacy_suite: BudgetedValidationSuite | None) -> BudgetedValidationHistory:
        identity = data["suite_identity"]
        runs = tuple(_decode_run(run, legacy_suite) for run in data["runs"])
        if any(run is None for run in runs):
            raise ValueError("Invalid null validation run")
        history = BudgetedValidationHistory(
            suite_identity=identity, last_success=_decode_run(data["last_success"], legacy_suite),
            latest=_decode_run(data["latest"], legacy_suite),
            last_scheduled=_decode_run(data["last_scheduled"], legacy_suite),
            runs=tuple(run for run in runs if run is not None),
            first_bad_commit=data["first_bad_commit"], diagnosis=data["diagnosis"],
            regression=_decode_regression(data, legacy_suite),
        )
        if legacy_suite is None and any(
            suite_identity(run.suite) != identity for run in history.runs
        ):
            raise ValueError("validation history contains a run from another suite definition")
        return history

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
        self._write_json(self._path(suite), {"version": 3, **asdict(history)})

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


def _decode_regression(data: dict, legacy_suite: BudgetedValidationSuite | None) -> BudgetedValidationRegression | None:
    if data["version"] == 1:
        # Explicit v1 migration: recover an unreported confirmed failure even
        # when latest is an interrupted diagnosis or unavailable scheduled run.
        completed = [run for raw in data["runs"] if (run := _decode_run(raw, legacy_suite)) is not None
                     and run.purpose == "scheduled" and run.finished_at is not None]
        last_green = None
        pending = None
        for run in completed:
            if run.probe.is_success:
                last_green, pending = run.probe.commit, None
            elif run.probe.is_failure and pending is None:
                pending = BudgetedValidationRegression(run, last_green)
        return pending
    raw = data["regression"]
    if raw is None:
        return None
    failed = _decode_run(raw["failed"], legacy_suite)
    if failed is None:
        raise ValueError("regression lacks a failed run")
    return BudgetedValidationRegression(failed, raw["last_green_commit"],
        raw["first_bad_commit"], raw["diagnosis"])
