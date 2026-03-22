from __future__ import annotations

import json
from pathlib import Path

from issue_orchestrator.execution.validation_failure_summary import load_validation_failure_summary


def test_load_validation_failure_summary_extracts_failed_tests_and_excerpts(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)

    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_name": "coding-1",
                "run_dir": str(run_dir),
                "worktree": str(worktree),
                "validation_status": "failed",
                "validation_reason": "Validation failed for deadbeef (exit_code=2)",
                "validation_record_path": ".issue-orchestrator/sessions/run-1/validation-record.json",
                "validation_stdout": ".issue-orchestrator/sessions/run-1/validation-stdout.log",
                "validation_stderr": ".issue-orchestrator/sessions/run-1/validation-stderr.log",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "validation-record.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite": "publish_gate",
                "head_sha": "deadbeef",
                "passed": False,
                "exit_code": 2,
                "command": "make validate",
                "started_at": "2026-03-22T04:53:14Z",
                "ended_at": "2026-03-22T04:53:58Z",
                "timed_out": False,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "validation-stdout.log").write_text(
        "\n".join(
            [
                "[gw1] [ 99%] PASSED tests/unit/test_ok.py::test_ok",
                "=================================== FAILURES ===================================",
                "_______ TestProviderCircuitsEndpoint.test_get_provider_circuits_open _________",
                "tests/unit/test_web.py:6936: in test_get_provider_circuits_open",
                "    assert len(data[\"circuits\"]) == 1",
                "E   assert 0 == 1",
                "FAILED tests/unit/test_web.py::TestProviderCircuitsEndpoint::test_get_provider_circuits_open",
                "FAILED tests/unit/test_web.py::TestProviderCircuitsEndpoint::test_get_provider_circuits_expired",
                "============================= slowest 10 durations =============================",
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "validation-stderr.log").write_text(
        "make: *** [validate] Error 2\nerror: failed to push some refs",
        encoding="utf-8",
    )

    summary = load_validation_failure_summary(run_dir)

    assert summary is not None
    assert summary.reason == "Validation failed for deadbeef (exit_code=2)"
    assert summary.suite == "publish_gate"
    assert summary.command == "make validate"
    assert summary.exit_code == 2
    assert summary.failed_tests == (
        "tests/unit/test_web.py::TestProviderCircuitsEndpoint::test_get_provider_circuits_open",
        "tests/unit/test_web.py::TestProviderCircuitsEndpoint::test_get_provider_circuits_expired",
    )
    assert any("FAILURES" in line for line in summary.stdout_excerpt)
    assert summary.stderr_excerpt[-1] == "error: failed to push some refs"


def test_load_validation_failure_summary_returns_none_for_non_failed_runs(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-2"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_name": "coding-2",
                "run_dir": str(run_dir),
                "worktree": str(worktree),
                "validation_status": "passed",
            }
        ),
        encoding="utf-8",
    )

    assert load_validation_failure_summary(run_dir) is None
