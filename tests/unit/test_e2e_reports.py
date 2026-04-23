"""Unit tests for generic E2E result report parsing."""

from pathlib import Path

import pytest

from issue_orchestrator.infra.e2e_reports import (
    JUnitCaseResult,
    normalize_pytest_junit_cases,
    parse_junit_report,
)


def test_parse_junit_report_extracts_cases(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        """\
<testsuite name="suite">
  <testcase classname="pkg.test_mod" name="test_passes" time="1.25" />
  <testcase classname="pkg.test_mod" name="test_fails" time="2.50">
    <failure message="AssertionError">expected 1, got 2</failure>
  </testcase>
  <testcase classname="pkg.test_mod" name="test_skipped">
    <skipped message="not enabled" />
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )

    cases = parse_junit_report(report)

    assert [case.case_id for case in cases] == [
        "pkg.test_mod::test_passes",
        "pkg.test_mod::test_fails",
        "pkg.test_mod::test_skipped",
    ]
    assert cases[0].outcome == "passed"
    assert cases[0].duration_seconds == 1.25
    assert cases[1].outcome == "failed"
    assert cases[1].failure_summary == "AssertionError"
    assert "expected 1, got 2" in (cases[1].failure_details or "")
    assert cases[2].outcome == "skipped"


def test_parse_junit_report_rejects_missing_testcases(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text("<testsuite />", encoding="utf-8")

    with pytest.raises(ValueError, match="did not contain any <testcase>"):
        parse_junit_report(report)


def test_parse_junit_report_rejects_missing_case_name(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text("<testsuite><testcase classname='pkg.test_mod' /></testsuite>", encoding="utf-8")

    with pytest.raises(ValueError, match="missing a non-empty name"):
        parse_junit_report(report)


def test_normalize_pytest_junit_cases_matches_runtime_nodeids() -> None:
    normalized = normalize_pytest_junit_cases(
        [
            JUnitCaseResult(
                case_id="tests.e2e.test_basic::test_passing",
                display_name="test_passing",
                suite_name="tests.e2e.test_basic",
                outcome="passed",
                duration_seconds=0.12,
            ),
            JUnitCaseResult(
                case_id="tests/e2e/test_existing.py::test_already_normalized",
                display_name="test_already_normalized",
                suite_name="tests/e2e/test_existing.py",
                outcome="failed",
                duration_seconds=0.34,
                failure_details="boom",
            ),
        ]
    )

    assert normalized[0].suite_name == "tests/e2e/test_basic.py"
    assert normalized[0].case_id == "tests/e2e/test_basic.py::test_passing"
    assert normalized[1].suite_name == "tests/e2e/test_existing.py"
    assert normalized[1].case_id == "tests/e2e/test_existing.py::test_already_normalized"
