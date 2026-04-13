from __future__ import annotations

from pathlib import Path
import sys

import pytest

from issue_orchestrator.testing.support.portfolio_benchmark import (
    PortfolioBenchmarkReport,
    build_pytest_command,
    list_cases,
    parse_junit_report,
    render_markdown,
    select_cases,
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
    cases = select_cases(["happy_path_pr", "publish_failure"])
    report = PortfolioBenchmarkReport(
        generated_at="2026-04-02T12:00:00+00:00",
        repo_root=tmp_path,
        output_dir=tmp_path / "artifacts",
        command=["python", "-m", "pytest"],
        pytest_exit_code=1,
        results=tuple(
            parse_junit_report(
                tmp_path / "missing-junit.xml",
                cases,
            )
        ),
    )

    markdown = render_markdown(report)

    assert "# Applied AI Portfolio Benchmark" in markdown
    assert "| Case | Status | Capability | Claim | Why It Matters |" in markdown
    assert "`happy_path_pr`" not in markdown
    assert "happy_path_pr" in markdown
    assert "publish_failure" in markdown
    assert "summary.json" in markdown
    assert "pytest-command.txt" in markdown


def test_list_cases_mentions_available_benchmark_cases() -> None:
    rendered = list_cases()

    assert "Available portfolio benchmark cases:" in rendered
    assert "happy_path_pr" in rendered
    assert "review_rework" in rendered
