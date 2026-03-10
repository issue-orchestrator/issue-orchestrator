"""E2E test results database - SQLite-based persistence.

Stores E2E test run results and per-test outcomes for dashboard visibility.
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .sqlite_connection import open_sqlite

logger = logging.getLogger(__name__)


# SQLite schema for e2e results
_SCHEMA = """
CREATE TABLE IF NOT EXISTS e2e_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_root TEXT NOT NULL,
    orchestrator_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    exit_code INTEGER,
    pytest_args TEXT NOT NULL,
    commit_sha TEXT,
    branch TEXT,
    retry_of INTEGER,
    is_retry_run INTEGER DEFAULT 0,
    duration_seconds REAL,
    note TEXT,
    log_path TEXT,
    artifacts_dir TEXT,
    worker_pid INTEGER,
    total_tests INTEGER,
    current_test TEXT,
    orchestrator_instance_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS e2e_test_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    nodeid TEXT NOT NULL,
    outcome TEXT NOT NULL,
    duration_seconds REAL,
    longrepr TEXT,
    retry_outcome TEXT,
    is_quarantined INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES e2e_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_e2e_runs_orch_started
    ON e2e_runs(orchestrator_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_e2e_test_results_run
    ON e2e_test_results(run_id, outcome);

CREATE UNIQUE INDEX IF NOT EXISTS idx_e2e_test_results_run_nodeid
    ON e2e_test_results(run_id, nodeid);

-- E2E Issue Tracking: Links test failures to GitHub issues
CREATE TABLE IF NOT EXISTS e2e_failure_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nodeid TEXT NOT NULL,
    github_issue_number INTEGER NOT NULL,
    parent_issue_number INTEGER NOT NULL,
    first_failing_run_id INTEGER NOT NULL,
    first_failing_sha TEXT NOT NULL,
    last_passing_sha TEXT,
    resolved_at TEXT,
    resolution TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(first_failing_run_id) REFERENCES e2e_runs(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_e2e_failure_issues_nodeid_sha
    ON e2e_failure_issues(nodeid, first_failing_sha);

CREATE INDEX IF NOT EXISTS idx_e2e_failure_issues_parent
    ON e2e_failure_issues(parent_issue_number);

-- E2E Issue Tracking: Tracks E2E run parent issues
CREATE TABLE IF NOT EXISTS e2e_run_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    github_issue_number INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    FOREIGN KEY(run_id) REFERENCES e2e_runs(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_e2e_run_issues_run
    ON e2e_run_issues(run_id);

-- E2E Flakiness Tracking: Records flaky test occurrences
CREATE TABLE IF NOT EXISTS e2e_flake_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nodeid TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    was_flaky INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES e2e_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_e2e_flake_history_nodeid
    ON e2e_flake_history(nodeid, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_e2e_test_results_nodeid
    ON e2e_test_results(nodeid);

-- E2E Run Events: Captures SSE events from the orchestrator during each run.
-- Enables timeline rendering using the same model as the main UI's issue detail.
CREATE TABLE IF NOT EXISTS e2e_run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event_name TEXT NOT NULL,
    source_event TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES e2e_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_e2e_run_events_run_id
    ON e2e_run_events(run_id, id);
"""


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


@dataclass
class E2ERunEvent:
    """An SSE event captured during an E2E test run.

    Enables timeline rendering for E2E runs using the same model as the main
    UI's issue detail view.
    """

    id: int
    run_id: int
    event_id: str
    timestamp: str
    event_name: str
    source_event: str
    data: dict

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "E2ERunEvent":
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            event_id=row["event_id"],
            timestamp=row["timestamp"],
            event_name=row["event_name"],
            source_event=row["source_event"],
            data=json.loads(row["data_json"]),
        )

    def to_timeline_dict(self) -> dict:
        """Convert to a dict compatible with the main UI's timeline rendering.

        Maps E2E run events to the TimelineEvent shape so the same
        frontend components can render both issue timelines and E2E run timelines.
        """
        phase = _e2e_event_phase(self.event_name)
        status = _e2e_event_status(self.event_name, self.data)
        level = _e2e_event_level(self.event_name)
        summary = _e2e_event_summary(self.event_name, self.data)

        d: dict = {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event": self.event_name,
            "issue_number": 0,
            "phase": phase,
            "step": self.event_name.split(".")[-1] if "." in self.event_name else self.event_name,
            "status": status,
            "level": level,
            "summary": summary,
            "parent_key": f"e2e-run-{self.run_id}",
            "detail": None,
            "run_id": str(self.run_id),
            "artifacts": [],
            "unsupported_schema": False,
            "review_oriented": False,
            "event_intent": "system",
            "source_event": self.source_event or None,
        }
        # Expose nodeid for reliable correlation in _build_test_windows
        nodeid = self.data.get("nodeid")
        if isinstance(nodeid, str):
            d["nodeid"] = nodeid
        return d


def _e2e_event_phase(event_name: str) -> str:
    """Map E2E event names to timeline phases."""
    if event_name in ("e2e.run_started", "e2e.tests_collected"):
        return "setup"
    if event_name.startswith("e2e.test_"):
        return "execution"
    if event_name == "e2e.retry_started":
        return "retry"
    return "teardown"  # run_finished, run_canceled, run_error


def _e2e_event_status(event_name: str, data: dict | None = None) -> str:
    if event_name in ("e2e.run_error", "e2e.run_canceled"):
        return "error"
    if event_name == "e2e.test_completed" and data:
        outcome = data.get("outcome", "")
        if outcome in ("failed", "error"):
            return "error"
        if outcome == "skipped":
            return "skipped"
        return "completed"
    if event_name == "e2e.test_completed":
        return "completed"
    if event_name == "e2e.run_finished":
        return "completed"
    return "active"


def _e2e_event_level(event_name: str) -> str:
    if event_name in ("e2e.run_error",):
        return "error"
    if event_name in ("e2e.run_canceled",):
        return "warning"
    if "test_completed" in event_name:
        return "detail"
    return "info"


def _e2e_event_summary(event_name: str, data: dict) -> str:
    if event_name == "e2e.run_started":
        branch = data.get("branch", "unknown")
        return f"E2E run started on {branch}"
    if event_name == "e2e.tests_collected":
        return f"Collected {data.get('total', '?')} tests"
    if event_name == "e2e.test_started":
        return data.get("nodeid", "")
    if event_name == "e2e.test_completed":
        outcome = data.get("outcome", "?")
        nodeid = data.get("nodeid", "")
        dur = data.get("duration_seconds")
        dur_str = f" ({dur:.1f}s)" if dur else ""
        return f"{nodeid}: {outcome}{dur_str}"
    if event_name == "e2e.retry_started":
        return f"Retrying {data.get('failed_count', '?')} failed tests"
    if event_name == "e2e.run_finished":
        return f"Run {data.get('status', '?')} in {data.get('duration_seconds', '?')}s"
    if event_name == "e2e.run_canceled":
        return "Run canceled"
    if event_name == "e2e.run_error":
        return f"Run error: {data.get('error', 'unknown')[:100]}"
    return event_name


def e2e_run_timeline(
    events: list[E2ERunEvent],
    orchestrator_events: list[dict] | None = None,
) -> dict:
    """Build a hierarchical timeline for an E2E run.

    Pytest-level events (test_started, test_completed, run_started, etc.) form the
    top layer.  Orchestrator events from timeline.sqlite are nested as ``children``
    under whichever test was active when they occurred (matched by timestamp window
    between test_started and test_completed).

    Orchestrator events that fall outside any test window (e.g. during setup/teardown)
    are attached as children of the nearest preceding pytest event.

    Args:
        events: Pytest-level E2E run events from e2e.db.
        orchestrator_events: Pre-converted timeline event dicts read from
            timeline.sqlite.  Each dict should already be in the main-UI timeline
            event shape (as produced by ``TimelineEvent.to_dict()``).
    """
    pytest_dicts = [e.to_timeline_dict() for e in events]

    if not orchestrator_events:
        return {"events": pytest_dicts}

    # Build timestamp windows for test events (test_started → test_completed pairs)
    _nest_orchestrator_events(pytest_dicts, orchestrator_events)

    return {"events": pytest_dicts}


def _build_test_windows(
    pytest_events: list[dict],
) -> list[tuple[str, str, dict]]:
    """Pair test_started/test_completed into (start_ts, end_ts, parent_dict) windows."""
    windows: list[tuple[str, str, dict]] = []
    started_map: dict[str, tuple[str, dict]] = {}

    for evt in pytest_events:
        evt.setdefault("children", [])
        name = evt.get("event", "")
        if name == "e2e.test_started":
            nodeid = (evt.get("nodeid") or "").strip()
            if nodeid:
                started_map[nodeid] = (evt["timestamp"], evt)
        elif name == "e2e.test_completed":
            nodeid = (evt.get("nodeid") or "").strip()
            if nodeid and nodeid in started_map:
                start_ts, started_dict = started_map.pop(nodeid)
                windows.append((start_ts, evt["timestamp"], started_dict))
    return windows


def _find_nearest_preceding(pytest_events: list[dict], ts: str) -> dict | None:
    """Return the nearest pytest event whose timestamp <= ts, or None."""
    best = None
    for p_evt in pytest_events:
        if p_evt.get("timestamp", "") <= ts:
            best = p_evt
        else:
            break  # pytest_events are in chronological order
    return best


def _nest_orchestrator_events(
    pytest_events: list[dict],
    orch_events: list[dict],
) -> None:
    """Mutate *pytest_events* in-place, adding ``children`` lists.

    Strategy:
    1. Pair test_started / test_completed into windows keyed by nodeid.
    2. For each orchestrator event, find the window whose start <= ts <= end.
    3. If no window matches, attach to the nearest preceding pytest event.
    """
    windows = _build_test_windows(pytest_events)
    sorted_orch = sorted(orch_events, key=lambda e: e.get("timestamp", ""))

    for orch_evt in sorted_orch:
        ts = orch_evt.get("timestamp", "")
        parent = _find_window_parent(windows, ts) or _find_nearest_preceding(pytest_events, ts)
        if parent is not None:
            parent["children"].append(orch_evt)
        elif pytest_events:
            pytest_events[0]["children"].append(orch_evt)


def _find_window_parent(
    windows: list[tuple[str, str, dict]], ts: str,
) -> dict | None:
    """Return the parent dict for the window containing ts, or None."""
    for start_ts, end_ts, parent_evt in windows:
        if start_ts <= ts <= end_ts:
            return parent_evt
    return None


@dataclass
class TestStability:
    """Flip-rate stability analysis for a single test.

    A "flip" is when a test's outcome changes between consecutive runs
    (pass->fail or fail->pass). High flip rate = flaky.
    """

    __test__ = False  # Not a pytest test class

    nodeid: str
    flip_rate: float  # 0.0 to 1.0
    flip_count: int  # Number of flips in the window
    run_count: int  # Number of runs in the window
    category: str  # flaky, consistently_failing, new_failure, recovered, healthy
    is_likely_flaky: bool  # flip_rate >= threshold
    recent_outcomes: list[str]  # Most recent first: ["passed", "failed", ...]

    @property
    def flip_rate_percent(self) -> float:
        """Flip rate as a percentage (0-100)."""
        return round(self.flip_rate * 100, 1)

    def to_dict(self) -> dict:
        return {
            "nodeid": self.nodeid,
            "flip_rate": self.flip_rate,
            "flip_rate_percent": self.flip_rate_percent,
            "flip_count": self.flip_count,
            "run_count": self.run_count,
            "category": self.category,
            "is_likely_flaky": self.is_likely_flaky,
            "recent_outcomes": self.recent_outcomes,
        }


def _compute_stability(
    nodeid: str,
    outcomes: list[str],
    threshold_percent: float,
) -> TestStability:
    """Compute flip-rate stability for a test from its recent outcomes.

    Pure function — no DB access. Easy to test.

    Args:
        nodeid: Test node ID
        outcomes: Recent outcomes, most recent first (e.g., ["passed", "failed", ...])
        threshold_percent: Flip rate percentage (0-100) above which test is flaky
    """
    if not outcomes:
        return TestStability(
            nodeid=nodeid,
            flip_rate=0.0,
            flip_count=0,
            run_count=0,
            category="healthy",
            is_likely_flaky=False,
            recent_outcomes=[],
        )

    # Count flips: consecutive outcome changes
    flip_count = 0
    for i in range(1, len(outcomes)):
        if outcomes[i] != outcomes[i - 1]:
            flip_count += 1

    # Flip rate: flips / (n-1) possible transitions
    max_transitions = len(outcomes) - 1
    flip_rate = flip_count / max_transitions if max_transitions > 0 else 0.0

    is_likely_flaky = (flip_rate * 100) >= threshold_percent

    category = _categorize_test(outcomes, is_likely_flaky)

    return TestStability(
        nodeid=nodeid,
        flip_rate=flip_rate,
        flip_count=flip_count,
        run_count=len(outcomes),
        category=category,
        is_likely_flaky=is_likely_flaky,
        recent_outcomes=outcomes,
    )


def _categorize_test(outcomes: list[str], is_likely_flaky: bool) -> str:
    """Categorize a test based on its recent outcomes.

    Pure function — no DB access.

    Categories:
        flaky: flip_rate >= threshold
        consistently_failing: low flip rate, most recent is failure, not flaky
        new_failure: < 3 runs of history, recent failure
        recovered: most recent is pass, but had failures in window
        healthy: all passes or no history
    """
    if not outcomes:
        return "healthy"

    if is_likely_flaky:
        return "flaky"

    most_recent = outcomes[0]
    has_failures = any(o == "failed" for o in outcomes)

    if most_recent == "failed":
        if len(outcomes) < 3:
            return "new_failure"
        return "consistently_failing"

    # Most recent is pass
    if has_failures:
        return "recovered"

    return "healthy"


def _categorize_test_results(
    results: list["E2ETestResult"],
    history_by_nodeid: dict[str, list[dict]],
    issues_by_nodeid: dict[str, dict],
    flake_threshold_percent: float,
) -> dict[str, list[dict]]:
    """Categorize test results into groups for the unified run view.

    Categories:
        untriaged: consistently failing, no issue
        has_issue: consistently failing, issue exists
        flaky: unstable history (any outcome this run)
        fixed: passed, has open issue to close
        passed: stable passing
        quarantined: quarantined tests
        skipped: skipped tests
    """
    tests_by_category: dict[str, list[dict]] = {
        "untriaged": [],
        "has_issue": [],
        "flaky": [],
        "fixed": [],
        "passed": [],
        "quarantined": [],
        "skipped": [],
    }

    for result in results:
        test_dict = _build_enhanced_test_dict(
            result, history_by_nodeid, issues_by_nodeid, flake_threshold_percent
        )
        category = _determine_test_category(result, test_dict)
        tests_by_category[category].append(test_dict)

    return tests_by_category


def _build_enhanced_test_dict(
    result: "E2ETestResult",
    history_by_nodeid: dict[str, list[dict]],
    issues_by_nodeid: dict[str, dict],
    flake_threshold_percent: float,
) -> dict:
    """Build enhanced test dict with history, issue info, and stability data."""
    nodeid = result.nodeid
    test_dict = result.to_dict()

    # Add history
    history = history_by_nodeid.get(nodeid, [])
    test_dict["history"] = history

    # Add existing issue info
    existing_issue = issues_by_nodeid.get(nodeid)
    test_dict["existing_issue"] = existing_issue

    # Determine effective outcome for this run
    effective_outcome = result.retry_outcome or result.outcome

    # Build outcomes list for stability calculation (this run + history)
    all_outcomes = [effective_outcome] + [h["outcome"] for h in history]
    stability = _compute_stability(nodeid, all_outcomes, flake_threshold_percent)
    test_dict["category"] = stability.category
    test_dict["flip_rate"] = stability.flip_rate
    test_dict["flip_rate_percent"] = stability.flip_rate_percent
    test_dict["is_likely_flaky"] = stability.is_likely_flaky

    return test_dict


def _determine_test_category(
    result: "E2ETestResult",
    test_dict: dict,
) -> str:
    """Determine which category a test belongs to for grouping."""
    if result.is_quarantined:
        return "quarantined"
    if result.outcome == "skipped":
        return "skipped"
    if test_dict["is_likely_flaky"]:
        return "flaky"

    effective_outcome = result.retry_outcome or result.outcome
    existing_issue = test_dict.get("existing_issue")
    has_open_issue = existing_issue and existing_issue["status"] == "open"

    if effective_outcome == "passed":
        return "fixed" if has_open_issue else "passed"
    # Failed this run
    return "has_issue" if has_open_issue else "untriaged"


class AlreadyRunning(Exception):
    """Raised when attempting to start a run while one is already running."""

    def __init__(self, orchestrator_id: str, existing_run_id: int):
        self.orchestrator_id = orchestrator_id
        self.existing_run_id = existing_run_id
        super().__init__(
            f"E2E run already in progress for {orchestrator_id} (run_id={existing_run_id})"
        )


class E2EDB:
    """SQLite database for E2E test results.

    Thread-safe through connection-per-operation pattern.
    """

    def __init__(self, db_path: Path):
        """Initialize E2E database.

        Args:
            db_path: Path to SQLite database file. Created if doesn't exist.
        """
        self.db_path = db_path
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create tables if they don't exist, and migrate existing ones."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Migrate: add orchestrator_instance_id if missing (pre-existing DBs)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(e2e_runs)")}
            if "orchestrator_instance_id" not in columns:
                conn.execute(
                    "ALTER TABLE e2e_runs ADD COLUMN orchestrator_instance_id TEXT NOT NULL DEFAULT ''"
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Get a database connection with row factory."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_sqlite(self.db_path, timeout=10.0, row_factory=sqlite3.Row)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _now_iso(self) -> str:
        """Return current UTC time as ISO string."""
        return datetime.now(timezone.utc).isoformat()

    # -------------------------------------------------------------------------
    # Run lifecycle
    # -------------------------------------------------------------------------

    def start_run(
        self,
        repo_root: str,
        orchestrator_id: str,
        pytest_args: list[str],
        commit_sha: Optional[str] = None,
        branch: Optional[str] = None,
        retry_of: Optional[int] = None,
        worker_pid: Optional[int] = None,
        orchestrator_instance_id: str = "",
    ) -> int:
        """Start a new E2E run.

        Enforces single running run per orchestrator_id. If an existing run
        has a dead worker process, it's marked as 'interrupted' (resumable).

        Args:
            repo_root: Path to repo root
            orchestrator_id: Unique orchestrator identifier
            pytest_args: Arguments to pass to pytest
            commit_sha: Git commit SHA (optional)
            branch: Git branch name (optional)
            retry_of: If this is a retry, the original run_id
            worker_pid: PID of the worker process (for orphan detection)

        Returns:
            The new run's ID

        Raises:
            AlreadyRunning: If a run is already in progress for this orchestrator
        """
        with self._connect() as conn:
            # Check for existing running run
            cursor = conn.execute(
                """
                SELECT id, worker_pid FROM e2e_runs
                WHERE orchestrator_id = ? AND status = 'running'
                LIMIT 1
                """,
                (orchestrator_id,),
            )
            row = cursor.fetchone()
            if row:
                existing_pid = row["worker_pid"]
                # Check if the worker process is still alive
                if existing_pid and not self._is_process_alive(existing_pid):
                    # Orphaned run - mark as interrupted (resumable)
                    conn.execute(
                        """
                        UPDATE e2e_runs SET
                            status = 'interrupted',
                            finished_at = ?,
                            note = ?
                        WHERE id = ?
                        """,
                        (self._now_iso(), f"Worker process died (PID {existing_pid})", row["id"]),
                    )
                    logger.warning(
                        "Marked orphaned E2E run %d as interrupted (PID %s dead)",
                        row["id"],
                        existing_pid,
                    )
                else:
                    raise AlreadyRunning(orchestrator_id, row["id"])

            # Create new run
            cursor = conn.execute(
                """
                INSERT INTO e2e_runs (
                    repo_root, orchestrator_id, started_at, status,
                    pytest_args, commit_sha, branch, retry_of, is_retry_run,
                    worker_pid, orchestrator_instance_id
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo_root,
                    orchestrator_id,
                    self._now_iso(),
                    json.dumps(pytest_args),
                    commit_sha,
                    branch,
                    retry_of,
                    1 if retry_of else 0,
                    worker_pid,
                    orchestrator_instance_id,
                ),
            )
            run_id = cursor.lastrowid
            assert run_id is not None, "INSERT failed to return lastrowid"
            logger.info(
                "Started E2E run %d for %s (commit=%s, branch=%s, pid=%s)",
                run_id,
                orchestrator_id,
                commit_sha,
                branch,
                worker_pid,
            )
            return run_id

    def _is_process_alive(self, pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)  # Signal 0 doesn't kill, just checks
            return True
        except OSError:
            return False

    def update_worker_pid(self, run_id: int, worker_pid: int) -> None:
        """Update the worker PID for a run (called after subprocess starts)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE e2e_runs SET worker_pid = ? WHERE id = ?",
                (worker_pid, run_id),
            )

    def update_progress(
        self,
        run_id: int,
        total_tests: Optional[int] = None,
        current_test: Optional[str] = None,
    ) -> None:
        """Update progress info for a running test.

        Args:
            run_id: The run to update
            total_tests: Total tests collected (set once after collection)
            current_test: Currently executing test nodeid
        """
        with self._connect() as conn:
            if total_tests is not None and current_test is not None:
                conn.execute(
                    "UPDATE e2e_runs SET total_tests = ?, current_test = ? WHERE id = ?",
                    (total_tests, current_test, run_id),
                )
            elif total_tests is not None:
                conn.execute(
                    "UPDATE e2e_runs SET total_tests = ? WHERE id = ?",
                    (total_tests, run_id),
                )
            elif current_test is not None:
                conn.execute(
                    "UPDATE e2e_runs SET current_test = ? WHERE id = ?",
                    (current_test, run_id),
                )

    def get_progress(self, run_id: int) -> dict:
        """Get progress stats for a run.

        Returns:
            Dict with total_tests, completed, passed, failed, skipped, current_test
        """
        with self._connect() as conn:
            # Get run info
            cursor = conn.execute(
                "SELECT total_tests, current_test FROM e2e_runs WHERE id = ?",
                (run_id,),
            )
            row = cursor.fetchone()
            if not row:
                return {}

            total_tests = row["total_tests"]
            current_test = row["current_test"]

            # Count results by outcome
            cursor = conn.execute(
                """
                SELECT outcome, COUNT(*) as cnt
                FROM e2e_test_results
                WHERE run_id = ?
                GROUP BY outcome
                """,
                (run_id,),
            )
            counts = {r["outcome"]: r["cnt"] for r in cursor.fetchall()}

            passed = counts.get("passed", 0)
            failed = counts.get("failed", 0)
            skipped = counts.get("skipped", 0)
            error = counts.get("error", 0)
            completed = passed + failed + skipped + error

            return {
                "total_tests": total_tests,
                "completed": completed,
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "error": error,
                "current_test": current_test,
                "percent": round(completed / total_tests * 100) if total_tests else None,
            }

    def finish_run(
        self,
        run_id: int,
        status: str,
        exit_code: Optional[int] = None,
        duration_seconds: Optional[float] = None,
        log_path: Optional[str] = None,
        artifacts_dir: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        """Mark a run as finished.

        Args:
            run_id: The run to finish
            status: Final status (passed, failed, canceled, error)
            exit_code: Pytest exit code
            duration_seconds: Total run duration
            log_path: Path to log file
            artifacts_dir: Path to artifacts directory
            note: Optional note
        """
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE e2e_runs SET
                    finished_at = ?,
                    status = ?,
                    exit_code = ?,
                    duration_seconds = ?,
                    log_path = ?,
                    artifacts_dir = ?,
                    note = ?
                WHERE id = ?
                """,
                (
                    self._now_iso(),
                    status,
                    exit_code,
                    duration_seconds,
                    log_path,
                    artifacts_dir,
                    note,
                    run_id,
                ),
            )
            logger.info("Finished E2E run %d with status=%s", run_id, status)

    def cancel_running(self, orchestrator_id: str) -> Optional[int]:
        """Cancel any running run for an orchestrator.

        Returns:
            The canceled run's ID, or None if no running run
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT id FROM e2e_runs
                WHERE orchestrator_id = ? AND status = 'running'
                LIMIT 1
                """,
                (orchestrator_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            run_id = row["id"]
            conn.execute(
                """
                UPDATE e2e_runs SET
                    finished_at = ?,
                    status = 'canceled',
                    note = 'Canceled by user'
                WHERE id = ?
                """,
                (self._now_iso(), run_id),
            )
            logger.info("Canceled E2E run %d for %s", run_id, orchestrator_id)
            return run_id

    # -------------------------------------------------------------------------
    # Resume support
    # -------------------------------------------------------------------------

    def get_interrupted_run(self, orchestrator_id: str) -> Optional[E2ERun]:
        """Get the most recent interrupted run that can be resumed."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_runs
                WHERE orchestrator_id = ? AND status = 'interrupted'
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (orchestrator_id,),
            )
            row = cursor.fetchone()
            return E2ERun.from_row(row) if row else None

    def get_passed_nodeids(self, run_id: int) -> set[str]:
        """Get nodeids that passed in a run (for resume - skip these tests)."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT nodeid FROM e2e_test_results
                WHERE run_id = ? AND outcome = 'passed'
                """,
                (run_id,),
            )
            return {row["nodeid"] for row in cursor.fetchall()}

    def resume_run(self, run_id: int, worker_pid: int) -> bool:
        """Resume an interrupted run.

        Args:
            run_id: The interrupted run to resume
            worker_pid: PID of the new worker process

        Returns:
            True if resumed successfully, False if run wasn't interrupted
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT status FROM e2e_runs WHERE id = ?",
                (run_id,),
            )
            row = cursor.fetchone()
            if not row or row["status"] != "interrupted":
                return False

            conn.execute(
                """
                UPDATE e2e_runs SET
                    status = 'running',
                    worker_pid = ?,
                    note = COALESCE(note, '') || ' | Resumed'
                WHERE id = ?
                """,
                (worker_pid, run_id),
            )
            logger.info("Resumed E2E run %d with worker PID %d", run_id, worker_pid)
            return True

    # -------------------------------------------------------------------------
    # Test results
    # -------------------------------------------------------------------------

    def upsert_test_result(
        self,
        run_id: int,
        nodeid: str,
        outcome: str,
        duration_seconds: Optional[float] = None,
        longrepr: Optional[str] = None,
        retry_outcome: Optional[str] = None,
        is_quarantined: bool = False,
    ) -> None:
        """Insert or update a test result.

        Uses UPSERT to handle retries updating the same nodeid.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO e2e_test_results (
                    run_id, nodeid, outcome, duration_seconds,
                    longrepr, retry_outcome, is_quarantined, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, nodeid) DO UPDATE SET
                    outcome = excluded.outcome,
                    duration_seconds = excluded.duration_seconds,
                    longrepr = excluded.longrepr,
                    retry_outcome = excluded.retry_outcome,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    nodeid,
                    outcome,
                    duration_seconds,
                    longrepr,
                    retry_outcome,
                    1 if is_quarantined else 0,
                    self._now_iso(),
                ),
            )

    def update_retry_outcome(
        self, run_id: int, nodeid: str, retry_outcome: str
    ) -> None:
        """Update just the retry outcome for a test."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE e2e_test_results SET
                    retry_outcome = ?,
                    updated_at = ?
                WHERE run_id = ? AND nodeid = ?
                """,
                (retry_outcome, self._now_iso(), run_id, nodeid),
            )

    # -------------------------------------------------------------------------
    # Queries
    # -------------------------------------------------------------------------

    def latest_run(self, orchestrator_id: str) -> Optional[E2ERun]:
        """Get the most recent run for an orchestrator."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_runs
                WHERE orchestrator_id = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (orchestrator_id,),
            )
            row = cursor.fetchone()
            return E2ERun.from_row(row) if row else None

    def get_running(self, orchestrator_id: str) -> Optional[E2ERun]:
        """Get the currently running run for an orchestrator, if any."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_runs
                WHERE orchestrator_id = ? AND status = 'running'
                LIMIT 1
                """,
                (orchestrator_id,),
            )
            row = cursor.fetchone()
            return E2ERun.from_row(row) if row else None

    def list_runs(self, orchestrator_id: str, limit: int = 20) -> list[E2ERun]:
        """List recent runs for an orchestrator."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_runs
                WHERE orchestrator_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (orchestrator_id, limit),
            )
            return [E2ERun.from_row(row) for row in cursor.fetchall()]

    def get_run(self, run_id: int) -> Optional[E2ERun]:
        """Get a run by ID.

        Args:
            run_id: Run ID

        Returns:
            E2ERun or None if not found
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM e2e_runs WHERE id = ?", (run_id,)
            )
            row = cursor.fetchone()
            return E2ERun.from_row(row) if row else None

    def run_details(self, run_id: int) -> Optional[dict]:
        """Get a run with its test results.

        Returns:
            Dict with 'run' and 'results' keys, or None if not found
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM e2e_runs WHERE id = ?", (run_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None

            run = E2ERun.from_row(row)

            cursor = conn.execute(
                """
                SELECT * FROM e2e_test_results
                WHERE run_id = ?
                ORDER BY nodeid
                """,
                (run_id,),
            )
            results = [E2ETestResult.from_row(r) for r in cursor.fetchall()]

            return {
                "run": run.to_dict(),
                "results": [r.to_dict() for r in results],
            }

    def _fetch_test_history(
        self,
        conn: sqlite3.Connection,
        nodeids: list[str],
        run_id: int,
        history_limit: int,
    ) -> dict[str, list[dict]]:
        """Batch fetch test outcome history for all nodeids.

        Uses SQL window function to limit rows per nodeid at the database level,
        avoiding fetching unnecessary data for large test histories.
        """
        if not nodeids:
            return {}

        from collections import defaultdict

        placeholders = ",".join("?" * len(nodeids))
        cursor = conn.execute(
            f"""
            WITH ranked_history AS (
                SELECT
                    t.nodeid,
                    COALESCE(t.retry_outcome, t.outcome) AS effective_outcome,
                    r.id AS run_id,
                    ROW_NUMBER() OVER (PARTITION BY t.nodeid ORDER BY r.started_at DESC) AS rn
                FROM e2e_test_results t
                JOIN e2e_runs r ON t.run_id = r.id
                WHERE t.nodeid IN ({placeholders})
                    AND r.id != ?
                    AND r.status IN ('passed', 'failed')
            )
            SELECT nodeid, effective_outcome, run_id
            FROM ranked_history
            WHERE rn <= ?
            ORDER BY nodeid
            """,
            (*nodeids, run_id, history_limit),
        )
        history: dict[str, list[dict]] = defaultdict(list)
        for row in cursor.fetchall():
            history[row["nodeid"]].append({
                "outcome": row["effective_outcome"],
                "run_id": row["run_id"],
            })
        return dict(history)

    def _fetch_issue_info(
        self,
        conn: sqlite3.Connection,
        nodeids: list[str],
    ) -> dict[str, dict]:
        """Batch fetch failure issue info for all nodeids."""
        if not nodeids:
            return {}

        issues_by_nodeid: dict[str, dict] = {}
        placeholders = ",".join("?" * len(nodeids))
        cursor = conn.execute(
            f"""
            SELECT nodeid, github_issue_number, resolution, resolved_at
            FROM e2e_failure_issues
            WHERE nodeid IN ({placeholders})
            ORDER BY nodeid, created_at DESC
            """,
            tuple(nodeids),
        )
        # Take the most recent issue per nodeid
        for issue_row in cursor.fetchall():
            nodeid = issue_row["nodeid"]
            if nodeid not in issues_by_nodeid:
                issues_by_nodeid[nodeid] = {
                    "number": issue_row["github_issue_number"],
                    "status": "closed" if issue_row["resolved_at"] else "open",
                    "resolution": issue_row["resolution"],
                }
        return issues_by_nodeid

    def run_details_enhanced(
        self,
        run_id: int,
        history_limit: int = 5,
        flake_threshold_percent: float = 20.0,
    ) -> Optional[dict]:
        """Get enhanced run details with test history, issue info, and categories.

        This is used by the unified run view to display all information needed
        for triaging E2E failures without additional API calls.

        Returns:
            Dict with:
                - run: Run metadata
                - tests_by_category: Tests grouped by state (untriaged, has_issue, flaky, fixed, passed)
                - summary: Counts for each category
        """
        with self._connect() as conn:
            # Get run
            cursor = conn.execute(
                "SELECT * FROM e2e_runs WHERE id = ?", (run_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None

            run = E2ERun.from_row(row)

            # Get all test results for this run
            cursor = conn.execute(
                "SELECT * FROM e2e_test_results WHERE run_id = ? ORDER BY nodeid",
                (run_id,),
            )
            results = [E2ETestResult.from_row(r) for r in cursor.fetchall()]

            # Batch fetch history and issues
            nodeids = [r.nodeid for r in results]
            history_by_nodeid = self._fetch_test_history(conn, nodeids, run_id, history_limit)
            issues_by_nodeid = self._fetch_issue_info(conn, nodeids)

            # Build enhanced test data and categorize
            tests_by_category = _categorize_test_results(
                results, history_by_nodeid, issues_by_nodeid, flake_threshold_percent
            )

            # Build summary counts
            summary = {cat: len(tests) for cat, tests in tests_by_category.items()}
            summary["total"] = len(results)

            return {
                "run": run.to_dict(),
                "tests_by_category": tests_by_category,
                "summary": summary,
            }

    def get_failed_tests(self, run_id: int) -> list[E2ETestResult]:
        """Get just the failed tests from a run (excluding quarantined)."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_test_results
                WHERE run_id = ?
                    AND outcome = 'failed'
                    AND is_quarantined = 0
                    AND (retry_outcome IS NULL OR retry_outcome = 'failed')
                ORDER BY nodeid
                """,
                (run_id,),
            )
            return [E2ETestResult.from_row(row) for row in cursor.fetchall()]

    def get_test_result(self, run_id: int, nodeid: str) -> E2ETestResult | None:
        """Get a specific test result by run ID and nodeid."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM e2e_test_results WHERE run_id = ? AND nodeid = ?",
                (run_id, nodeid),
            )
            row = cursor.fetchone()
            return E2ETestResult.from_row(row) if row else None

    def get_test_history(self, nodeid: str, limit: int = 10) -> list[dict]:
        """Get historical results for a specific test across recent runs.

        Returns list of dicts with run_id, outcome, started_at for display.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT
                    r.id AS run_id,
                    t.outcome,
                    t.retry_outcome,
                    r.started_at
                FROM e2e_test_results t
                JOIN e2e_runs r ON t.run_id = r.id
                WHERE t.nodeid = ?
                ORDER BY r.started_at DESC
                LIMIT ?
                """,
                (nodeid, limit),
            )
            results = []
            for row in cursor.fetchall():
                # Determine effective outcome (retry_outcome takes precedence)
                outcome = row["retry_outcome"] or row["outcome"]
                results.append({
                    "run_id": row["run_id"],
                    "outcome": outcome,
                    "started_at": row["started_at"],
                })
            return results

    def get_test_summary(self, run_id: int) -> dict:
        """Get comprehensive test summary for a run.

        Returns:
            Dict with:
                - passed: tests that passed (or passed on retry)
                - failed: tests that failed (even after retry, non-quarantined)
                - passed_on_retry: tests that failed first but passed on retry
                - quarantined: quarantined tests (excluded from failure count)
                - skipped: skipped tests
                - counts: summary counts
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM e2e_test_results WHERE run_id = ? ORDER BY nodeid",
                (run_id,),
            )
            results = [E2ETestResult.from_row(row) for row in cursor.fetchall()]

        passed = []
        failed = []
        passed_on_retry = []
        quarantined = []
        skipped = []

        for r in results:
            if r.is_quarantined:
                quarantined.append(r)
            elif r.outcome == "skipped":
                skipped.append(r)
            elif r.outcome == "passed":
                passed.append(r)
            elif r.outcome == "failed":
                if r.retry_outcome == "passed":
                    passed_on_retry.append(r)
                else:
                    failed.append(r)

        return {
            "passed": [t.to_dict() for t in passed],
            "failed": [t.to_dict() for t in failed],
            "passed_on_retry": [t.to_dict() for t in passed_on_retry],
            "quarantined": [t.to_dict() for t in quarantined],
            "skipped": [t.to_dict() for t in skipped],
            "counts": {
                "total": len(results),
                "passed": len(passed),
                "failed": len(failed),
                "passed_on_retry": len(passed_on_retry),
                "quarantined": len(quarantined),
                "skipped": len(skipped),
            },
        }

    # -------------------------------------------------------------------------
    # Signal score
    # -------------------------------------------------------------------------

    def compute_signal_score(
        self, orchestrator_id: str, last_n_runs: int = 30
    ) -> dict:
        """Compute stability metrics for an orchestrator.

        Returns:
            Dict with pass_rate, runs_analyzed, quarantined_count
        """
        with self._connect() as conn:
            # Get last N completed runs
            cursor = conn.execute(
                """
                SELECT id, status FROM e2e_runs
                WHERE orchestrator_id = ? AND status != 'running'
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (orchestrator_id, last_n_runs),
            )
            runs = cursor.fetchall()

            if not runs:
                return {
                    "pass_rate": None,
                    "runs_analyzed": 0,
                    "quarantined_count": 0,
                }

            passed = sum(1 for r in runs if r["status"] == "passed")
            pass_rate = passed / len(runs)

            # Count quarantined tests from most recent run
            latest_run_id = runs[0]["id"] if runs else None
            quarantined_count = 0
            if latest_run_id:
                cursor = conn.execute(
                    """
                    SELECT COUNT(*) as cnt FROM e2e_test_results
                    WHERE run_id = ? AND is_quarantined = 1
                    """,
                    (latest_run_id,),
                )
                quarantined_count = cursor.fetchone()["cnt"]

            return {
                "pass_rate": pass_rate,
                "runs_analyzed": len(runs),
                "quarantined_count": quarantined_count,
            }

    # -------------------------------------------------------------------------
    # E2E Issue Tracking Methods
    # -------------------------------------------------------------------------

    def record_run_issue(
        self,
        run_id: int,
        github_issue_number: int,
    ) -> int:
        """Record a GitHub parent issue for an E2E run.

        Args:
            run_id: E2E run ID
            github_issue_number: GitHub issue number for the parent issue

        Returns:
            ID of the created record
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO e2e_run_issues (run_id, github_issue_number, created_at)
                VALUES (?, ?, ?)
                """,
                (run_id, github_issue_number, self._now_iso()),
            )
            return cursor.lastrowid or 0

    def get_run_issue(self, run_id: int) -> Optional[E2ERunIssue]:
        """Get the GitHub issue for an E2E run.

        Args:
            run_id: E2E run ID

        Returns:
            E2ERunIssue or None if no issue exists
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM e2e_run_issues WHERE run_id = ?",
                (run_id,),
            )
            row = cursor.fetchone()
            return E2ERunIssue.from_row(row) if row else None

    def close_run_issue(self, run_id: int) -> bool:
        """Mark a run's GitHub issue as closed.

        Args:
            run_id: E2E run ID

        Returns:
            True if updated, False if no issue existed
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE e2e_run_issues SET closed_at = ?
                WHERE run_id = ? AND closed_at IS NULL
                """,
                (self._now_iso(), run_id),
            )
            return cursor.rowcount > 0

    def get_open_run_issues(self) -> list[E2ERunIssue]:
        """Get all open (not closed) E2E run issues.

        Returns:
            List of E2ERunIssue records where closed_at IS NULL
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM e2e_run_issues WHERE closed_at IS NULL ORDER BY created_at DESC",
            )
            return [E2ERunIssue.from_row(row) for row in cursor.fetchall()]

    def record_failure_issue(
        self,
        nodeid: str,
        github_issue_number: int,
        parent_issue_number: int,
        first_failing_run_id: int,
        first_failing_sha: str,
        last_passing_sha: Optional[str] = None,
    ) -> int:
        """Record a GitHub sub-issue for a test failure.

        Args:
            nodeid: Test node ID (e.g., tests/e2e/test_foo.py::test_bar)
            github_issue_number: GitHub issue number for the sub-issue
            parent_issue_number: Parent issue number
            first_failing_run_id: Run ID where failure was first detected
            first_failing_sha: Commit SHA where failure was first detected
            last_passing_sha: Last known passing commit SHA

        Returns:
            ID of the created record
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO e2e_failure_issues
                (nodeid, github_issue_number, parent_issue_number,
                 first_failing_run_id, first_failing_sha, last_passing_sha, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    nodeid,
                    github_issue_number,
                    parent_issue_number,
                    first_failing_run_id,
                    first_failing_sha,
                    last_passing_sha,
                    self._now_iso(),
                ),
            )
            return cursor.lastrowid or 0

    def find_open_failure_issue(
        self,
        nodeid: str,
    ) -> Optional[E2EFailureIssue]:
        """Find an open GitHub issue for a test failure.

        Args:
            nodeid: Test node ID

        Returns:
            E2EFailureIssue or None if no open issue exists
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_failure_issues
                WHERE nodeid = ? AND resolved_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (nodeid,),
            )
            row = cursor.fetchone()
            return E2EFailureIssue.from_row(row) if row else None

    def resolve_failure_issue(
        self,
        nodeid: str,
        resolution: str,
    ) -> bool:
        """Mark a failure issue as resolved.

        Args:
            nodeid: Test node ID
            resolution: Resolution type ('passed', 'quarantined', 'manual')

        Returns:
            True if updated, False if no open issue existed
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE e2e_failure_issues
                SET resolved_at = ?, resolution = ?
                WHERE nodeid = ? AND resolved_at IS NULL
                """,
                (self._now_iso(), resolution, nodeid),
            )
            return cursor.rowcount > 0

    def get_failure_issues_for_parent(
        self,
        parent_issue_number: int,
    ) -> list[E2EFailureIssue]:
        """Get all failure sub-issues for a parent issue.

        Args:
            parent_issue_number: Parent GitHub issue number

        Returns:
            List of E2EFailureIssue records
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_failure_issues
                WHERE parent_issue_number = ?
                ORDER BY nodeid
                """,
                (parent_issue_number,),
            )
            return [E2EFailureIssue.from_row(row) for row in cursor.fetchall()]

    def get_unresolved_failure_count(
        self,
        parent_issue_number: int,
    ) -> int:
        """Count unresolved failure issues for a parent.

        Args:
            parent_issue_number: Parent GitHub issue number

        Returns:
            Count of unresolved failure issues
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT COUNT(*) as cnt FROM e2e_failure_issues
                WHERE parent_issue_number = ? AND resolved_at IS NULL
                """,
                (parent_issue_number,),
            )
            return cursor.fetchone()["cnt"]

    def get_all_open_failure_issues(self) -> list[E2EFailureIssue]:
        """Get all unresolved failure issues across all runs.

        Returns:
            List of E2EFailureIssue records where resolved_at IS NULL
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM e2e_failure_issues
                WHERE resolved_at IS NULL
                ORDER BY nodeid
                """,
            )
            return [E2EFailureIssue.from_row(row) for row in cursor.fetchall()]

    # -------------------------------------------------------------------------
    # Flip-Rate Stability Methods
    # -------------------------------------------------------------------------

    def _get_recent_outcomes(
        self,
        nodeid: str,
        window_runs: int = 10,
    ) -> list[str]:
        """Get recent effective outcomes for a test across completed runs.

        Returns outcomes most-recent-first. Uses COALESCE(retry_outcome, outcome)
        to get the effective outcome (retry takes precedence). Only includes
        completed runs with pass/fail outcomes.

        Args:
            nodeid: Test node ID
            window_runs: Number of recent runs to include
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT COALESCE(t.retry_outcome, t.outcome) AS effective_outcome
                FROM e2e_test_results t
                JOIN e2e_runs r ON t.run_id = r.id
                WHERE t.nodeid = ?
                    AND r.status IN ('passed', 'failed')
                    AND COALESCE(t.retry_outcome, t.outcome) IN ('passed', 'failed')
                ORDER BY r.started_at DESC
                LIMIT ?
                """,
                (nodeid, window_runs),
            )
            return [row["effective_outcome"] for row in cursor.fetchall()]

    def get_test_stability(
        self,
        nodeid: str,
        window_runs: int = 10,
        flake_threshold_percent: float = 20.0,
    ) -> TestStability:
        """Get flip-rate stability analysis for a single test.

        Args:
            nodeid: Test node ID
            window_runs: Number of recent runs to analyze
            flake_threshold_percent: Flip rate percentage (0-100) to flag as flaky
        """
        outcomes = self._get_recent_outcomes(nodeid, window_runs)
        return _compute_stability(nodeid, outcomes, flake_threshold_percent)

    def get_all_test_stability(
        self,
        window_runs: int = 10,
        flake_threshold_percent: float = 20.0,
    ) -> list[TestStability]:
        """Get flip-rate stability for all tests with recent history.

        Single SQL call to fetch all outcomes, then compute stability per test.

        Args:
            window_runs: Number of recent runs to analyze per test
            flake_threshold_percent: Flip rate percentage (0-100) to flag as flaky
        """
        with self._connect() as conn:
            # Bulk query: get recent outcomes for all tests, ordered by test then recency
            cursor = conn.execute(
                """
                SELECT t.nodeid,
                       COALESCE(t.retry_outcome, t.outcome) AS effective_outcome,
                       r.started_at
                FROM e2e_test_results t
                JOIN e2e_runs r ON t.run_id = r.id
                WHERE r.status IN ('passed', 'failed')
                    AND COALESCE(t.retry_outcome, t.outcome) IN ('passed', 'failed')
                ORDER BY t.nodeid, r.started_at DESC
                """,
            )

            # Group outcomes by nodeid, limiting to window_runs per test
            from collections import defaultdict
            outcomes_by_nodeid: dict[str, list[str]] = defaultdict(list)
            for row in cursor.fetchall():
                nodeid_key = row["nodeid"]
                if len(outcomes_by_nodeid[nodeid_key]) < window_runs:
                    outcomes_by_nodeid[nodeid_key].append(row["effective_outcome"])

        results = []
        for nodeid_key, outcomes in outcomes_by_nodeid.items():
            stability = _compute_stability(nodeid_key, outcomes, flake_threshold_percent)
            results.append(stability)

        # Sort by flip_rate descending for most-flaky-first ordering
        results.sort(key=lambda s: s.flip_rate, reverse=True)
        return results


    # -------------------------------------------------------------------------
    # Run events (timeline)
    # -------------------------------------------------------------------------

    def append_run_event(
        self,
        run_id: int,
        event_id: str,
        timestamp: str,
        event_name: str,
        data: dict,
        *,
        source_event: str = "",
    ) -> None:
        """Capture an SSE event from the orchestrator during an E2E run.

        These events enable timeline rendering per-run using the same model
        as the main UI's issue detail view.
        """
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO e2e_run_events
                   (run_id, event_id, timestamp, event_name, source_event, data_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, event_id, timestamp, event_name, source_event, json.dumps(data)),
            )

    def get_run_events(self, run_id: int) -> list[E2ERunEvent]:
        """Get all captured SSE events for a run, ordered by insertion."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM e2e_run_events WHERE run_id = ? ORDER BY id",
                (run_id,),
            )
            return [E2ERunEvent.from_row(row) for row in cursor.fetchall()]

    def delete_run_events(self, run_id: int) -> int:
        """Delete all captured events for a run. Returns count deleted."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM e2e_run_events WHERE run_id = ?",
                (run_id,),
            )
            return cursor.rowcount


# -------------------------------------------------------------------------
# Utility functions
# -------------------------------------------------------------------------


def load_quarantine_list(quarantine_path: Path) -> set[str]:
    """Load quarantined test nodeids from a file.

    File format: one nodeid per line, lines starting with # are comments.

    Args:
        quarantine_path: Path to quarantine file

    Returns:
        Set of quarantined nodeids
    """
    if not quarantine_path.exists():
        return set()

    quarantined = set()
    with open(quarantine_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                quarantined.add(line)

    return quarantined


def save_quarantine_list(quarantine_path: Path, nodeids: set[str]) -> None:
    """Save the quarantine list to a file.

    Creates the file and parent directories if they don't exist.
    Preserves header comment if present.

    Args:
        quarantine_path: Path to quarantine file
        nodeids: Set of nodeids to quarantine
    """
    # Preserve any header comments if file exists
    header_lines = []
    if quarantine_path.exists():
        with open(quarantine_path) as f:
            for line in f:
                if line.startswith("#"):
                    header_lines.append(line.rstrip())
                else:
                    break

    # Ensure parent directory exists
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)

    with open(quarantine_path, "w") as f:
        # Write header if present
        if header_lines:
            for line in header_lines:
                f.write(line + "\n")
            f.write("\n")
        else:
            # Add a default header
            f.write("# Quarantined E2E tests\n")
            f.write("# Tests listed here are excluded from E2E failure counts\n")
            f.write("\n")

        # Write sorted nodeids
        for nodeid in sorted(nodeids):
            f.write(nodeid + "\n")
