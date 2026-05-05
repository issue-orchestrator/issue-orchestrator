"""End-to-end view-model test for validation failures.

The user-facing claim being tested:
    "When a test fails, the user can quickly see which tests failed,
    read the actual error/traceback for each, and understand what
    went wrong — without leaving the dashboard."

This test pins the chain that delivers that experience:

    validation-record.json + validation-stdout.log + JUnit XML
        │
        ├─ load_validation_failure_summary(run_dir)
        │       returns ValidationFailureSummary with
        │       - failed_tests: list of nodeids
        │       - stdout_excerpt: pytest FAILURES section
        │       - junit_cases: structured per-test outcomes
        │
        ├─ ValidationFailureSummary.to_dict()
        │       serialized into the manifest payload that the
        │       dashboard's session-manifest endpoint returns
        │
        └─ build_validation_failure_dialog(issue_number, payload)
                final view-model JSON the dialog renders from

If any link drops the per-test structured data, this test fails —
which is the cue to fix the source, not the test.

The fixture-style scenario uses synthetic input files (deterministic,
no real pytest run). The "B" pattern from the discussion — induce a
real failing test via the agent — is a separate scenario; this one
exercises the read-side chain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.execution.validation_failure_summary import (
    load_validation_failure_summary,
)
from issue_orchestrator.view_models.dialogs import build_validation_failure_dialog


_REALISTIC_STDOUT = """\
============================= test session starts ==============================
collected 12 items

tests/unit/test_foo.py::test_bar FAILED                                  [  8%]
tests/unit/test_baz.py::test_qux FAILED                                  [ 16%]
tests/unit/test_other.py::test_passes PASSED                             [ 25%]

=================================== FAILURES ===================================
___________________________________ test_bar ___________________________________

    def test_bar():
>       assert compute() == 2
E       AssertionError: expected 2, got 1
E       assert 1 == 2
E        +  where 1 = compute()

tests/unit/test_foo.py:42: AssertionError
___________________________________ test_qux ___________________________________

    def test_qux():
        result = lookup_user(None)
>       assert result["name"] == "Alice"
E       TypeError: 'NoneType' object is not subscriptable

tests/unit/test_baz.py:17: TypeError
=========================== short test summary info ============================
FAILED tests/unit/test_foo.py::test_bar - AssertionError: expected 2, got 1
FAILED tests/unit/test_baz.py::test_qux - TypeError: 'NoneType' object is not subscriptable
========================= 2 failed, 1 passed in 0.42s ==========================
"""


_REALISTIC_JUNIT_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="12" failures="2" errors="0" skipped="0" time="0.42">
    <testcase classname="tests.unit.test_foo" name="test_bar"
              file="tests/unit/test_foo.py" line="40" time="0.005">
      <failure message="AssertionError: expected 2, got 1">
def test_bar():
&gt;       assert compute() == 2
E       AssertionError: expected 2, got 1
E       assert 1 == 2
E        +  where 1 = compute()

tests/unit/test_foo.py:42: AssertionError
      </failure>
    </testcase>
    <testcase classname="tests.unit.test_baz" name="test_qux"
              file="tests/unit/test_baz.py" line="15" time="0.003">
      <failure message="TypeError: 'NoneType' object is not subscriptable">
def test_qux():
        result = lookup_user(None)
&gt;       assert result["name"] == "Alice"
E       TypeError: 'NoneType' object is not subscriptable

tests/unit/test_baz.py:17: TypeError
      </failure>
    </testcase>
    <testcase classname="tests.unit.test_other" name="test_passes" time="0.001"/>
  </testsuite>
</testsuites>
"""


@pytest.fixture
def failing_validation_run(tmp_path: Path) -> Path:
    """A run_dir that mirrors what the orchestrator produces after a
    failed `make test-unit` run, with both unstructured stdout and a
    JUnit XML report.

    Files written:
        run_dir/manifest.json              — RunManifest with validation_status=failed
        run_dir/validation-record.json     — exit_code, command, head_sha
        run_dir/validation-stdout.log      — pytest stdout incl. FAILURES section
        run_dir/junit.xml                  — structured per-test results
    """
    run_dir = tmp_path / "issue-1234-coding-1"
    run_dir.mkdir()

    manifest = {
        "schema_version": 1,
        "session_name": "issue-1234-coding-1",
        "issue_number": 1234,
        "agent": "coder",
        "task": "code",
        "started_at": "2026-05-04T00:00:00+00:00",
        "ended_at": "2026-05-04T00:01:00+00:00",
        "status": "failed",
        "worktree": str(tmp_path / "worktree"),
        "validation_status": "failed",
        "validation_reason": "2 unit tests failed: test_bar, test_qux",
        "validation_record_path": "validation-record.json",
        "validation_stdout": "validation-stdout.log",
        "validation_command": "make test-unit",
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))

    record = {
        "schema_version": 1,
        "suite": "publish_gate",
        "head_sha": "abc123def456",
        "passed": False,
        "exit_code": 1,
        "command": "make test-unit",
        "started_at": "2026-05-04T00:00:30+00:00",
        "ended_at": "2026-05-04T00:00:55+00:00",
        "timed_out": False,
        "stdout_path": "validation-stdout.log",
        "stderr_path": None,
    }
    (run_dir / "validation-record.json").write_text(json.dumps(record))

    (run_dir / "validation-stdout.log").write_text(_REALISTIC_STDOUT)

    # JUnit XML lives in the worktree path that load_validation_failure_summary
    # searches when junit_xml_paths is provided.
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "junit.xml").write_text(_REALISTIC_JUNIT_XML)

    return run_dir


def test_summary_extracts_failed_test_nodeids(failing_validation_run: Path) -> None:
    """The summary must surface which tests failed by nodeid."""
    summary = load_validation_failure_summary(failing_validation_run)
    assert summary is not None
    assert "tests/unit/test_foo.py::test_bar" in summary.failed_tests
    assert "tests/unit/test_baz.py::test_qux" in summary.failed_tests


def test_summary_extracts_pytest_failures_section(failing_validation_run: Path) -> None:
    """The stdout excerpt must include the pytest FAILURES section so the
    user can read the actual assertion errors and tracebacks."""
    summary = load_validation_failure_summary(failing_validation_run)
    assert summary is not None
    excerpt_text = "\n".join(summary.stdout_excerpt)
    assert "FAILURES" in excerpt_text
    assert "AssertionError: expected 2, got 1" in excerpt_text
    assert "TypeError: 'NoneType' object is not subscriptable" in excerpt_text


def test_summary_extracts_structured_junit_cases(failing_validation_run: Path) -> None:
    """When JUnit XML is available, structured per-test cases reach the
    summary so the dashboard can render a clean per-test table rather
    than only a stdout text blob."""
    summary = load_validation_failure_summary(
        failing_validation_run,
        junit_xml_paths=("junit.xml",),
    )
    assert summary is not None
    assert summary.junit_cases, "Expected JUnit cases to be parsed"
    case_ids = {case.case_id for case in summary.junit_cases}
    assert any("test_bar" in cid for cid in case_ids)
    assert any("test_qux" in cid for cid in case_ids)
    failed_cases = [c for c in summary.junit_cases if c.outcome == "failed"]
    assert len(failed_cases) == 2
    # Each failed case must carry the actual failure detail — the
    # actionable text the user reads to understand the failure.
    for case in failed_cases:
        assert case.failure_details, f"{case.case_id} missing failure_details"


def test_dialog_view_model_carries_actionable_test_info(failing_validation_run: Path) -> None:
    """End-to-end: the dialog payload the dashboard consumes must
    expose enough information for the user to identify the failing
    tests AND read their errors without leaving the dashboard.

    This is the user-facing claim. If this assertion fails, the
    fix is in the read-side chain (summary serialization → dialog
    builder), NOT in this test.
    """
    summary = load_validation_failure_summary(
        failing_validation_run,
        junit_xml_paths=("junit.xml",),
    )
    assert summary is not None

    payload = {
        "validation_failure": summary.to_dict(),
        "manifest": {
            "session_name": "issue-1234-coding-1",
            "issue_number": 1234,
            "validation_status": "failed",
            "validation_reason": summary.reason,
        },
    }
    dialog = build_validation_failure_dialog(1234, payload)

    # Reason / command / exit code: surface the basic context.
    assert dialog["reason"]
    assert dialog["command"] == "make test-unit"
    assert dialog["exit_code"] == 1

    # Failed tests: the user can see *which* tests failed.
    assert "tests/unit/test_foo.py::test_bar" in dialog["failed_tests"]
    assert "tests/unit/test_baz.py::test_qux" in dialog["failed_tests"]

    # Stdout excerpt: the user can read *why* they failed (assertions,
    # tracebacks). This is the unstructured fallback when JUnit data
    # isn't surfaced as structured cases.
    excerpt_text = "\n".join(dialog["stdout_excerpt"])
    assert "AssertionError: expected 2, got 1" in excerpt_text
    assert "TypeError: 'NoneType' object is not subscriptable" in excerpt_text


def test_dialog_view_model_exposes_structured_junit_cases(failing_validation_run: Path) -> None:
    """**The strong claim**: structured per-test cases reach the
    dashboard view-model so the dialog can render a per-test table
    where each row carries its own failure detail.

    Without this, users have to read through a stdout text blob to
    pair test names with failures. The user explicitly asked for
    'view actionable information per test'.

    If this fails, the fix is to serialize `junit_cases` from
    `ValidationFailureSummary.to_dict()` and surface them in
    `build_validation_failure_dialog`.
    """
    summary = load_validation_failure_summary(
        failing_validation_run,
        junit_xml_paths=("junit.xml",),
    )
    assert summary is not None
    assert summary.junit_cases, "Test setup wrong: no JUnit cases parsed"

    payload = {
        "validation_failure": summary.to_dict(),
        "manifest": {
            "session_name": "issue-1234-coding-1",
            "issue_number": 1234,
            "validation_status": "failed",
            "validation_reason": summary.reason,
        },
    }
    dialog = build_validation_failure_dialog(1234, payload)

    # The dialog SHOULD carry structured per-test cases. If this assertion
    # fails, junit_cases is being silently dropped between summary and
    # dialog — see `ValidationFailureSummary.to_dict()` and
    # `build_validation_failure_dialog`.
    junit_cases = dialog.get("junit_cases")
    assert junit_cases, (
        "Dialog payload is missing structured per-test cases. "
        "ValidationFailureSummary.junit_cases is computed but isn't "
        "reaching the dashboard view-model — check to_dict() and "
        "build_validation_failure_dialog. The user wanted per-test "
        "actionable info; without this, only the stdout text blob is "
        "available."
    )
    failed_cases = [c for c in junit_cases if c.get("outcome") == "failed"]
    assert len(failed_cases) >= 2

    # Each failed case must carry the actual failure text — the
    # actionable info the user reads.
    for case in failed_cases:
        assert case.get("failure_details"), (
            f"junit case {case.get('case_id')!r} is in the dialog but "
            "missing failure_details — the user can see the test name "
            "but cannot see why it failed without scrolling the stdout "
            "blob."
        )
