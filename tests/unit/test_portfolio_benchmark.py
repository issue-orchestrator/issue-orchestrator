from __future__ import annotations

import json
from pathlib import Path
import sys
from xml.etree import ElementTree

import pytest

from issue_orchestrator.testing.support.portfolio_benchmark import (
    PortfolioBenchmarkReport,
    build_pytest_command,
    list_cases,
    parse_benchmark_case,
    parse_junit_report,
    render_markdown,
    select_cases,
    write_report_artifacts,
)


def test_select_cases_rejects_unknown_case() -> None:
    with pytest.raises(ValueError, match="Unknown benchmark case"):
        select_cases(["not-a-real-case"])


def test_build_pytest_command_includes_targets_and_args(tmp_path: Path) -> None:
    cases = select_cases(["happy_path_pr", "review_rework"])

    command = build_pytest_command(
        junit_xml_path=tmp_path / "junit.xml",
        cases=cases,
        extra_pytest_args=["-x", "-vv"],
    )

    assert command[:6] == [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--junitxml",
        str(tmp_path / "junit.xml"),
    ]
    assert "-x" in command
    assert "-vv" in command
    assert command[-2:] == [
        "tests/simulated_scenarios/test_simulated_scenarios.py::test_local_loop_happy_path_creates_non_draft_pr",
        "tests/simulated_scenarios/test_simulated_scenarios.py::test_review_rework_then_approved",
    ]


def test_parse_junit_report_maps_result_statuses(tmp_path: Path) -> None:
    junit_xml = tmp_path / "junit.xml"
    junit_xml.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuite tests="3" failures="1" skipped="1">
  <testcase classname="tests.simulated_scenarios.test_simulated_scenarios" name="test_local_loop_happy_path_creates_non_draft_pr" time="1.25" />
  <testcase classname="tests.simulated_scenarios.test_simulated_scenarios" name="test_review_rework_then_approved" time="2.50">
    <failure message="review loop failed">traceback</failure>
  </testcase>
  <testcase classname="tests.simulated_scenarios.test_simulated_scenarios" name="test_completion_outcome_needs_human_sets_label_and_event" time="0.50">
    <skipped message="missing dependency">skipped in this environment</skipped>
  </testcase>
</testsuite>
"""
    )
    cases = select_cases(
        ["happy_path_pr", "review_rework", "needs_human", "restart_recovery"]
    )

    results = parse_junit_report(junit_xml, cases)

    assert [result.status for result in results] == [
        "passed",
        "failed",
        "skipped",
        "missing",
    ]
    assert results[1].detail == "review loop failed"
    assert results[2].detail == "missing dependency"
    assert results[3].detail == "Selected benchmark case was not present in junit.xml output."


def test_render_markdown_includes_claims_and_artifacts(tmp_path: Path) -> None:
    cases = select_cases(["happy_path_pr", "review_rework"])
    report = PortfolioBenchmarkReport(
        generated_at="2026-04-02T12:00:00+00:00",
        repo_name="issue-orchestrator",
        repo_root=tmp_path / "issue-orchestrator",
        output_dir=tmp_path / "issue-orchestrator" / ".issue-orchestrator" / "portfolio-benchmark" / "latest",
        command=["python", "-m", "pytest"],
        pytest_exit_code=1,
        results=tuple(
            [
                parse_junit_report(
                    tmp_path / "missing-junit.xml",
                    [cases[0]],
                )[0],
                parse_benchmark_case(
                    cases[1],
                    ElementTree.fromstring(
                        """<testcase name="test_review_rework_then_approved" time="2.5">
                        <failure>Long detail that should render in the details section.</failure>
                        </testcase>"""
                    ),
                ),
            ]
        ),
    )

    markdown = render_markdown(report)

    assert "# Applied AI Portfolio Benchmark" in markdown
    assert "| Case | Status | Capability | Claim | Why It Matters |" in markdown
    assert "happy_path_pr" in markdown
    assert "issue-orchestrator" in markdown
    assert ".issue-orchestrator/portfolio-benchmark/latest" in markdown
    assert str(tmp_path) not in markdown
    assert "## Details" in markdown
    assert "<details><summary><code>review_rework</code> detail</summary>" in markdown
    assert "summary.json" in markdown
    assert "pytest-command.txt" in markdown


def test_list_cases_mentions_available_benchmark_cases() -> None:
    rendered = list_cases()

    assert "Available portfolio benchmark cases:" in rendered
    assert "happy_path_pr" in rendered
    assert "review_rework" in rendered


def test_write_report_artifacts_writes_expected_files(tmp_path: Path) -> None:
    repo_root = tmp_path / "issue-orchestrator"
    output_dir = repo_root / ".issue-orchestrator" / "portfolio-benchmark" / "latest"
    output_dir.mkdir(parents=True)
    case = select_cases(["happy_path_pr"])[0]
    report = PortfolioBenchmarkReport(
        generated_at="2026-04-13T12:00:00+00:00",
        repo_name="issue-orchestrator",
        repo_root=repo_root,
        output_dir=output_dir,
        command=[
            str(repo_root / ".venv" / "bin" / "python"),
            "-m",
            "pytest",
            str(output_dir / "junit.xml"),
            case.pytest_target,
        ],
        pytest_exit_code=0,
        results=(parse_benchmark_case(case, None),),
    )

    write_report_artifacts(
        report=report,
        stdout="stdout text",
        stderr="stderr text",
    )

    summary = json.loads((output_dir / "summary.json").read_text())
    assert summary["repo"] == "issue-orchestrator"
    assert summary["output_dir"] == ".issue-orchestrator/portfolio-benchmark/latest"
    assert summary["command"] == [
        ".venv/bin/python",
        "-m",
        "pytest",
        ".issue-orchestrator/portfolio-benchmark/latest/junit.xml",
        "tests/simulated_scenarios/test_simulated_scenarios.py::test_local_loop_happy_path_creates_non_draft_pr",
    ]
    assert str(repo_root) not in (output_dir / "summary.md").read_text()
    assert str(repo_root) not in (output_dir / "summary.json").read_text()
    assert (output_dir / "pytest-command.txt").read_text() == (
        f"{repo_root / '.venv' / 'bin' / 'python'} -m pytest {output_dir / 'junit.xml'} "
        "tests/simulated_scenarios/test_simulated_scenarios.py::test_local_loop_happy_path_creates_non_draft_pr\n"
    )
    assert (output_dir / "pytest-stdout.txt").read_text() == "stdout text"
    assert (output_dir / "pytest-stderr.txt").read_text() == "stderr text"


def test_parse_benchmark_case_uses_summarized_failure_text_when_message_missing() -> None:
    case = select_cases(["publish_failure"])[0]
    testcase = ElementTree.fromstring(
        """<testcase name="test_processing_failure_push_error_marks_blocked_failed" time="1.0">
        <failure>
            First line

            Second line
        </failure>
        </testcase>"""
    )

    result = parse_benchmark_case(case, testcase)

    assert result.status == "failed"
    assert result.detail == "First line Second line"


def test_parse_benchmark_case_returns_none_detail_for_whitespace_only_failure_text() -> None:
    case = select_cases(["publish_failure"])[0]
    testcase = ElementTree.fromstring(
        """<testcase name="test_processing_failure_push_error_marks_blocked_failed" time="1.0">
        <failure>

        </failure>
        </testcase>"""
    )

    result = parse_benchmark_case(case, testcase)

    assert result.status == "failed"
    assert result.detail is None


def test_parse_benchmark_case_truncates_long_failure_text() -> None:
    case = select_cases(["publish_failure"])[0]
    long_text = " ".join(["detail"] * 60)
    testcase = ElementTree.fromstring(
        f"""<testcase name="test_processing_failure_push_error_marks_blocked_failed" time="1.0">
        <failure>{long_text}</failure>
        </testcase>"""
    )

    result = parse_benchmark_case(case, testcase)

    assert result.status == "failed"
    assert result.detail is not None
    assert len(result.detail) == 200
    assert result.detail.endswith("...")
