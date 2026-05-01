"""Unit tests for generic E2E result report parsing."""

from pathlib import Path

import pytest

from issue_orchestrator.infra.e2e_reports import (
    MAX_CAPTURED_OUTPUT_CHARS,
    JUnitCaseResult,
    discover_report_artifacts,
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


def test_parse_junit_report_preserves_error_outcome_under_testsuites_root(
    tmp_path: Path,
) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        """\
<testsuites>
  <testsuite name="suite">
    <testcase classname="pkg.test_mod" name="test_errors" time="0.50">
      <error message="RuntimeError">kaboom</error>
    </testcase>
  </testsuite>
</testsuites>
""",
        encoding="utf-8",
    )

    cases = parse_junit_report(report)

    assert len(cases) == 1
    assert cases[0].outcome == "error"
    assert cases[0].failure_summary == "RuntimeError"
    assert cases[0].failure_details == "RuntimeError\nkaboom"


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


def test_parse_junit_report_rejects_invalid_time_values(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        """\
<testsuite>
  <testcase classname="pkg.test_mod" name="test_bad_time" time="not-a-float" />
</testsuite>
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="could not convert string to float"):
        parse_junit_report(report)


def test_parse_junit_report_reports_parse_location(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text("<testsuite>\n  <testcase></testsuite>", encoding="utf-8")

    with pytest.raises(ValueError, match=r"Malformed JUnit XML: .*line 2, column 14"):
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


def test_discover_report_artifacts_classifies_and_dedupes_files(tmp_path: Path) -> None:
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    junit_report = reports_dir / "results.xml"
    junit_report.write_text(
        """\
<testsuite name="suite">
  <testcase classname="ui.smoke" name="test_homepage" time="1.0" />
</testsuite>
""",
        encoding="utf-8",
    )
    html_report = reports_dir / "report.html"
    html_report.write_text("<html><body>ok</body></html>", encoding="utf-8")
    summary = reports_dir / "summary.txt"
    summary.write_text("status=passed\n", encoding="utf-8")
    trace = reports_dir / "trace.zip"
    trace.write_bytes(b"PK\x03\x04")

    cases, artifacts = discover_report_artifacts(
        tmp_path,
        junit_xml_paths=["reports/results.xml"],
        artifact_paths=[
            "reports/report.html",
            "reports/*.txt",
            "reports/trace.zip",
            "reports/report.html",
        ],
    )

    assert [case.case_id for case in cases] == ["ui.smoke::test_homepage"]
    assert [(artifact.kind, artifact.label, Path(artifact.path).name) for artifact in artifacts] == [
        ("junit_xml", "JUnit XML: results.xml", "results.xml"),
        ("html_report", "HTML Report: report.html", "report.html"),
        ("text_artifact", "Text Artifact: summary.txt", "summary.txt"),
        ("trace", "Trace: trace.zip", "trace.zip"),
    ]


def test_discover_report_artifacts_rejects_missing_configured_artifact_path(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="Configured artifact paths did not resolve"):
        discover_report_artifacts(
            tmp_path,
            junit_xml_paths=[],
            artifact_paths=["reports/missing.log"],
        )


def test_parse_junit_report_extracts_system_out_and_system_err(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        """\
<testsuite name="suite">
  <testcase classname="pkg.test_mod" name="test_chatty" time="0.10">
    <system-out>captured stdout line 1
captured stdout line 2</system-out>
    <system-err>warning: something happened</system-err>
  </testcase>
  <testcase classname="pkg.test_mod" name="test_quiet" time="0.05" />
  <testcase classname="pkg.test_mod" name="test_blank_channels" time="0.05">
    <system-out>   </system-out>
    <system-err></system-err>
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )

    cases = parse_junit_report(report)

    assert cases[0].system_out == "captured stdout line 1\ncaptured stdout line 2"
    assert cases[0].system_err == "warning: something happened"
    assert cases[1].system_out is None
    assert cases[1].system_err is None
    assert cases[2].system_out is None
    assert cases[2].system_err is None


def test_parse_junit_report_caps_captured_output_at_limit(tmp_path: Path) -> None:
    huge = "x" * (MAX_CAPTURED_OUTPUT_CHARS + 5_000)
    report = tmp_path / "junit.xml"
    report.write_text(
        f"""\
<testsuite name="suite">
  <testcase classname="pkg.test_mod" name="test_loud" time="0.10">
    <system-out>{huge}</system-out>
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )

    cases = parse_junit_report(report)

    assert cases[0].system_out is not None
    assert len(cases[0].system_out) == MAX_CAPTURED_OUTPUT_CHARS


def test_parse_junit_report_attaches_captured_output_to_failed_case(
    tmp_path: Path,
) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        """\
<testsuite name="suite">
  <testcase classname="pkg.test_mod" name="test_fails" time="2.50">
    <failure message="AssertionError">expected 1, got 2</failure>
    <system-out>print before failure</system-out>
    <system-err>traceback noise</system-err>
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )

    cases = parse_junit_report(report)

    assert cases[0].outcome == "failed"
    assert cases[0].failure_summary == "AssertionError"
    assert cases[0].system_out == "print before failure"
    assert cases[0].system_err == "traceback noise"


def test_discover_report_artifacts_rejects_paths_outside_repo_root(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside-report.log"
    outside.write_text("hello\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside repo root"):
        discover_report_artifacts(
            tmp_path,
            junit_xml_paths=[],
            artifact_paths=[f"../{outside.name}"],
        )
