"""Tests for e2e_run_diagnosis module."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from issue_orchestrator.infra.e2e_run_diagnosis import (
    E2ERunDiagnosis,
    create_e2e_run_diagnosis,
    generate_diagnostic_issue_body,
    write_e2e_diagnostic,
    _build_warnings_and_suggestions,
    _read_log_content,
)
from issue_orchestrator.infra.e2e_db import E2ERun
from issue_orchestrator.infra.issue_diagnostics import DiagnosticReference

class TestE2ERunDiagnosis:
    """Tests for E2ERunDiagnosis dataclass."""

    def test_to_dict_includes_all_fields(self) -> None:
        """to_dict includes all diagnosis fields."""
        diagnosis = E2ERunDiagnosis(
            run_id=42,
            status="failed",
            commit_sha="abc123",
            branch="main",
            started_at="2024-01-01T00:00:00Z",
            finished_at="2024-01-01T00:05:00Z",
            duration_seconds=300.5,
            log_path="/path/to/log",
            log_exists=True,
            log_content="test output",
            total_tests=100,
            passed_count=90,
            failed_count=5,
            passed_on_retry_count=3,
            quarantined_count=1,
            skipped_count=1,
            failed_tests=[{"nodeid": "test_a", "longrepr": "error"}],
            flaky_tests=[{"nodeid": "test_b", "longrepr": "flaky"}],
            pytest_args=["tests/", "-v"],
            repo_root="/repo",
            orchestrator_id="orch-1",
            warnings=["High failure rate"],
            suggestions=["Check environment"],
        )

        d = diagnosis.to_dict()

        assert d["run_id"] == 42
        assert d["status"] == "failed"
        assert d["commit_sha"] == "abc123"
        assert d["branch"] == "main"
        assert d["duration_seconds"] == 300.5
        assert d["log_content"] == "test output"
        assert d["total_tests"] == 100
        assert d["passed_count"] == 90
        assert d["failed_count"] == 5
        assert d["passed_on_retry_count"] == 3
        assert d["quarantined_count"] == 1
        assert d["skipped_count"] == 1
        assert len(d["failed_tests"]) == 1
        assert len(d["flaky_tests"]) == 1
        assert d["warnings"] == ["High failure rate"]
        assert d["suggestions"] == ["Check environment"]

class TestReadLogContent:
    """Tests for _read_log_content function."""

    def test_returns_false_for_none_path(self) -> None:
        """Returns (False, None) when path is None."""
        exists, content = _read_log_content(None)
        assert exists is False
        assert content is None

    def test_returns_false_for_nonexistent_file(self, tmp_path: Path) -> None:
        """Returns (False, None) when file doesn't exist."""
        exists, content = _read_log_content(str(tmp_path / "nonexistent.log"))
        assert exists is False
        assert content is None

    def test_reads_full_log_content(self, tmp_path: Path) -> None:
        """Reads entire log file content."""
        log_file = tmp_path / "test.log"
        log_file.write_text("line 1\nline 2\nline 3\n")

        exists, content = _read_log_content(str(log_file))

        assert exists is True
        assert content == "line 1\nline 2\nline 3\n"

class TestBuildWarningsAndSuggestions:
    """Tests for _build_warnings_and_suggestions function."""

    def test_high_failure_rate_warning(self) -> None:
        """Warns when failure rate exceeds 50%."""
        run = MagicMock(spec=E2ERun)
        run.status = "failed"

        warnings, suggestions = _build_warnings_and_suggestions(
            run,
            failed_count=60,
            flaky_count=0,
            total_tests=100,
        )

        assert any("High failure rate" in w for w in warnings)
        assert any("environment" in s.lower() for s in suggestions)

    def test_elevated_failure_rate_warning(self) -> None:
        """Warns when failure rate exceeds 20%."""
        run = MagicMock(spec=E2ERun)
        run.status = "failed"

        warnings, _suggestions = _build_warnings_and_suggestions(
            run,
            failed_count=25,
            flaky_count=0,
            total_tests=100,
        )

        assert any("Elevated failure rate" in w for w in warnings)

    def test_flaky_tests_warning(self) -> None:
        """Warns when many tests are flaky."""
        run = MagicMock(spec=E2ERun)
        run.status = "failed"

        warnings, suggestions = _build_warnings_and_suggestions(
            run,
            failed_count=5,
            flaky_count=5,
            total_tests=100,
        )

        assert any("flaky" in w for w in warnings)
        assert any("quarantine" in s.lower() for s in suggestions)

    def test_interrupted_run_warning(self) -> None:
        """Warns when run was interrupted."""
        run = MagicMock(spec=E2ERun)
        run.status = "interrupted"

        warnings, suggestions = _build_warnings_and_suggestions(
            run,
            failed_count=0,
            flaky_count=0,
            total_tests=100,
        )

        assert any("interrupted" in w.lower() for w in warnings)
        assert any("OOM" in s or "timeout" in s for s in suggestions)

    def test_no_tests_collected_warning(self) -> None:
        """Warns when no tests were collected."""
        run = MagicMock(spec=E2ERun)
        run.status = "failed"

        warnings, _suggestions = _build_warnings_and_suggestions(
            run,
            failed_count=0,
            flaky_count=0,
            total_tests=0,
        )

        assert any("No tests were collected" in w for w in warnings)

class TestCreateE2ERunDiagnosis:
    """Tests for create_e2e_run_diagnosis function."""

    def test_returns_none_for_missing_run(self) -> None:
        """Returns None when run not found in database."""
        mock_db = MagicMock()
        mock_db.run_details.return_value = None

        result = create_e2e_run_diagnosis(999, mock_db)

        assert result is None

    def test_creates_diagnosis_from_db_data(self, tmp_path: Path) -> None:
        """Creates diagnosis with data from database."""
        # Create a log file
        log_file = tmp_path / "test.log"
        log_file.write_text("test log content\n")

        mock_db = MagicMock()
        mock_db.run_details.return_value = {
            "run": {
                "id": 1,
                "repo_root": str(tmp_path),
                "orchestrator_id": "test-orch",
                "started_at": "2024-01-01T00:00:00Z",
                "finished_at": "2024-01-01T00:05:00Z",
                "status": "failed",
                "exit_code": 1,
                "pytest_args": ["tests/"],
                "commit_sha": "abc123",
                "branch": "main",
                "retry_of": None,
                "is_retry_run": False,
                "duration_seconds": 300.0,
                "note": None,
                "log_path": str(log_file),
                "artifacts_dir": None,
                "worker_pid": 1234,
                "total_tests": 10,
                "current_test": None,
            },
            "results": [],
        }
        mock_db.get_test_summary.return_value = {
            "passed": [],
            "failed": [{"nodeid": "test_a", "longrepr": "error", "duration_seconds": 1.0}],
            "passed_on_retry": [],
            "quarantined": [],
            "skipped": [],
            "counts": {
                "total": 10,
                "passed": 9,
                "failed": 1,
                "passed_on_retry": 0,
                "quarantined": 0,
                "skipped": 0,
            },
        }

        diagnosis = create_e2e_run_diagnosis(1, mock_db)

        assert diagnosis is not None
        assert diagnosis.run_id == 1
        assert diagnosis.status == "failed"
        assert diagnosis.failed_count == 1
        assert diagnosis.log_exists is True
        assert diagnosis.log_content == "test log content\n"
        assert len(diagnosis.failed_tests) == 1

class TestWriteE2EDiagnostic:
    """Tests for write_e2e_diagnostic function."""

    def test_writes_diagnostic_file(self, tmp_path: Path) -> None:
        """Creates diagnostic JSON file in correct location."""
        diagnosis = E2ERunDiagnosis(
            run_id=42,
            status="failed",
            commit_sha="abc123",
            branch="main",
            started_at="2024-01-01T00:00:00Z",
            finished_at="2024-01-01T00:05:00Z",
            duration_seconds=300.0,
            log_path=None,
            log_exists=False,
            log_content=None,
            total_tests=10,
            passed_count=9,
            failed_count=1,
            passed_on_retry_count=0,
            quarantined_count=0,
            skipped_count=0,
        )

        ref = write_e2e_diagnostic(tmp_path, diagnosis)

        assert ref is not None
        assert ref.worktree_name == tmp_path.name
        assert "diagnostics" in ref.relative_path
        assert "e2e-run-42" in ref.relative_path

        # Verify file was written
        diag_path = tmp_path / ref.relative_path
        assert diag_path.exists()

        content = json.loads(diag_path.read_text())
        assert content["type"] == "e2e_run_diagnosis"
        assert content["run_id"] == 42
        assert content["diagnosis"]["status"] == "failed"

class TestGenerateDiagnosticIssueBody:
    """Tests for generate_diagnostic_issue_body function."""

    def test_includes_run_metadata(self) -> None:
        """Issue body includes run metadata."""
        diagnosis = E2ERunDiagnosis(
            run_id=42,
            status="failed",
            commit_sha="abc123",
            branch="main",
            started_at="2024-01-01T00:00:00Z",
            finished_at="2024-01-01T00:05:00Z",
            duration_seconds=300.0,
            log_path=None,
            log_exists=False,
            log_content=None,
            total_tests=10,
            passed_count=9,
            failed_count=1,
            passed_on_retry_count=0,
            quarantined_count=0,
            skipped_count=0,
        )

        body = generate_diagnostic_issue_body(diagnosis, None)

        assert "Run ID**: 42" in body
        assert "Status**: failed" in body
        assert "Commit**: abc123" in body
        assert "Branch**: main" in body

    def test_includes_test_results_table(self) -> None:
        """Issue body includes test results table."""
        diagnosis = E2ERunDiagnosis(
            run_id=1,
            status="failed",
            commit_sha=None,
            branch=None,
            started_at="2024-01-01T00:00:00Z",
            finished_at=None,
            duration_seconds=None,
            log_path=None,
            log_exists=False,
            log_content=None,
            total_tests=100,
            passed_count=90,
            failed_count=5,
            passed_on_retry_count=3,
            quarantined_count=1,
            skipped_count=1,
        )

        body = generate_diagnostic_issue_body(diagnosis, None)

        assert "| Total | 100 |" in body
        assert "| Passed | 90 |" in body
        assert "| Failed | 5 |" in body

    def test_includes_failed_tests_list(self) -> None:
        """Issue body lists failed tests."""
        diagnosis = E2ERunDiagnosis(
            run_id=1,
            status="failed",
            commit_sha=None,
            branch=None,
            started_at="2024-01-01T00:00:00Z",
            finished_at=None,
            duration_seconds=None,
            log_path=None,
            log_exists=False,
            log_content=None,
            total_tests=10,
            passed_count=8,
            failed_count=2,
            passed_on_retry_count=0,
            quarantined_count=0,
            skipped_count=0,
            failed_tests=[
                {"nodeid": "test_foo::test_bar", "longrepr": "error"},
                {"nodeid": "test_baz::test_qux", "longrepr": "error"},
            ],
        )

        body = generate_diagnostic_issue_body(diagnosis, None)

        assert "test_foo::test_bar" in body
        assert "test_baz::test_qux" in body

    def test_includes_diagnostic_file_reference(self) -> None:
        """Issue body includes reference to diagnostic file."""
        diagnosis = E2ERunDiagnosis(
            run_id=1,
            status="failed",
            commit_sha=None,
            branch=None,
            started_at="2024-01-01T00:00:00Z",
            finished_at=None,
            duration_seconds=None,
            log_path=None,
            log_exists=False,
            log_content=None,
            total_tests=10,
            passed_count=9,
            failed_count=1,
            passed_on_retry_count=0,
            quarantined_count=0,
            skipped_count=0,
        )
        ref = DiagnosticReference(
            worktree_name="test-repo",
            relative_path=".issue-orchestrator/diagnostics/test.json",
        )

        body = generate_diagnostic_issue_body(diagnosis, ref)

        assert ".issue-orchestrator/diagnostics/test.json" in body
        assert "Full Diagnostic Data" in body

    def test_includes_warnings_and_suggestions(self) -> None:
        """Issue body includes warnings and suggestions."""
        diagnosis = E2ERunDiagnosis(
            run_id=1,
            status="failed",
            commit_sha=None,
            branch=None,
            started_at="2024-01-01T00:00:00Z",
            finished_at=None,
            duration_seconds=None,
            log_path=None,
            log_exists=False,
            log_content=None,
            total_tests=10,
            passed_count=9,
            failed_count=1,
            passed_on_retry_count=0,
            quarantined_count=0,
            skipped_count=0,
            warnings=["High failure rate"],
            suggestions=["Check environment"],
        )

        body = generate_diagnostic_issue_body(diagnosis, None)

        assert "High failure rate" in body
        assert "Check environment" in body
