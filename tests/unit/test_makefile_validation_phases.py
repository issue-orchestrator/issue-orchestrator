"""Tests for Makefile validation phase orchestration."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _gnu_make() -> str:
    make_bin = shutil.which("gmake") or shutil.which("make")
    if make_bin is None:
        pytest.fail("GNU make is required to validate Makefile targets")
    result = subprocess.run(
        [make_bin, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or "GNU Make" not in result.stdout:
        pytest.fail("GNU make is required to validate Makefile targets")
    return make_bin


def _dry_run(target: str, **overrides: str) -> list[str]:
    env = dict(os.environ)
    env.pop("MAKEFLAGS", None)
    env.update(
        {
            "VALIDATE_JOBS": "10",
            "VALIDATE_TEST_JOBS": "1",
            "VALIDATE_WEB_JOBS": "1",
            "VALIDATE_AGENT_JOBS": "1",
            "VALIDATE_E2E_JOBS": "1",
            **overrides,
        }
    )
    result = subprocess.run(
        [_gnu_make(), "-n", "--always-make", target],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _matching_indexes(lines: list[str], *fragments: str) -> list[int]:
    return [
        index
        for index, line in enumerate(lines)
        if all(fragment in line for fragment in fragments)
    ]


def _find_line(lines: list[str], *fragments: str) -> int:
    matches = _matching_indexes(lines, *fragments)
    if not matches:
        raise AssertionError(
            f"Missing line containing {fragments!r}. Output:\n" + "\n".join(lines)
        )
    if len(matches) > 1:
        raise AssertionError(
            f"Expected one line containing {fragments!r}, got {len(matches)}"
        )
    return matches[0]


def _assert_job_count(line: str, jobs: int) -> None:
    assert re.search(rf"(?:^|\s)-j\s*{jobs}(?:\s|$)", line), line


def _assert_no_job_count(line: str) -> None:
    assert not re.search(r"(?:^|\s)-j\s*\d+(?:\s|$)", line), line


def test_validate_impl_runs_core_phases_with_separate_job_caps():
    lines = _dry_run("_validate-impl")

    static_index = _find_line(lines, "_validate-static-impl")
    core_tests_index = _find_line(lines, "_validate-core-tests-impl")
    web_index = _find_line(lines, "test-web")

    _assert_job_count(lines[static_index], 10)
    _assert_job_count(lines[core_tests_index], 1)
    _assert_job_count(lines[web_index], 1)

    assert static_index < core_tests_index < web_index


def test_validate_pr_impl_runs_agent_phase_after_validate_phase():
    lines = _dry_run("_validate-pr-impl")

    validate_index = _find_line(lines, "_validate-impl")
    agent_index = _find_line(lines, "_validate-agent-impl")

    _assert_job_count(lines[agent_index], 1)

    assert validate_index < agent_index


def test_validate_full_impl_runs_e2e_after_pr_phase():
    lines = _dry_run("_validate-full-impl")

    pr_index = _find_line(lines, "_validate-pr-impl")
    e2e_index = _find_line(lines, "test-e2e")

    _assert_job_count(lines[e2e_index], 1)

    assert pr_index < e2e_index


def test_validate_pr_raw_does_not_schedule_entire_graph_at_validate_jobs():
    lines = _dry_run("validate-pr-raw")
    raw_pr_index = _find_line(lines, "_validate-pr-impl")

    _assert_no_job_count(lines[raw_pr_index])


def test_validate_pr_uses_cache_aware_verify_script():
    lines = _dry_run("validate-pr")

    verify_index = _find_line(lines, "./scripts/verify-pr.sh")

    assert all("validate_runner" not in line for line in lines[: verify_index + 1])


def test_agent_validation_targets_emit_timing_markers():
    simulated_lines = _dry_run("test-simulated-agent", SIMULATED_PARALLEL="0")
    integration_lines = _dry_run("test-integration-agent", INTEGRATION_AGENT_PARALLEL="0")

    _find_line(simulated_lines, "[validate-timing] START target=$target")
    _find_line(simulated_lines, "[validate-timing] END target=$target")
    _find_line(simulated_lines, 'target="test-simulated-agent"')

    starts = _matching_indexes(integration_lines, "[validate-timing] START target=$target")
    ends = _matching_indexes(integration_lines, "[validate-timing] END target=$target")
    assert len(starts) == 1
    assert len(ends) == 1

    agent_index = _find_line(integration_lines, 'target="test-integration-agent"')
    assert starts == [agent_index]
    assert all(
        'target="test-integration-agent-live-codex"' not in line
        for line in integration_lines
    )
    assert all("live_codex" not in line for line in integration_lines)


def test_core_validation_runs_live_codex_marker_serially():
    lines = _dry_run("test-integration-core", INTEGRATION_PARALLEL="0")

    starts = _matching_indexes(lines, "[validate-timing] START target=$target")
    ends = _matching_indexes(lines, "[validate-timing] END target=$target")
    assert len(starts) == 2
    assert len(ends) == 2

    core_index = _find_line(lines, 'target="test-integration-core"')
    live_codex_index = _find_line(lines, 'target="test-integration-core-live-codex"')
    non_live_marker_index = _find_line(
        lines,
        '-m "not requires_infra and not live_codex"',
    )
    live_marker_index = _find_line(lines, '-m "live_codex and not requires_infra"')

    assert core_index < live_codex_index
    assert non_live_marker_index == core_index
    assert live_marker_index == live_codex_index
    assert all(
        "::test_real_interactive_codex_reviewer_round_trips_through_exchange" not in line
        for line in lines
    )


def test_agent_backed_integration_runs_serial_by_default():
    lines = _dry_run("test-integration-agent")
    pytest_line = lines[
        _find_line(
            lines,
            "tests/integration/test_claude_execution.py",
            "tests/integration/test_codex_execution.py",
            "tests/integration/test_live_agent_chain.py",
        )
    ]

    assert " -n " not in f" {pytest_line} "
    assert " -m " not in f" {pytest_line} "
    assert all("test-integration-agent-live-codex" not in line for line in lines)


def test_agent_backed_integration_allows_explicit_parallel_override():
    lines = _dry_run("test-integration-agent", INTEGRATION_AGENT_PARALLEL="2")
    pytest_line = lines[
        _find_line(
            lines,
            "tests/integration/test_claude_execution.py",
            "tests/integration/test_codex_execution.py",
            "tests/integration/test_live_agent_chain.py",
        )
    ]

    assert " -n 2 " in f" {pytest_line} "
    assert " -m " not in f" {pytest_line} "
    assert all("test-integration-agent-live-codex" not in line for line in lines)


def test_live_agent_transport_is_scheduled_by_e2e_not_agent_integration():
    integration_lines = _dry_run("test-integration-agent")
    e2e_lines = _dry_run("test-e2e")

    assert all(
        "tests/e2e/test_live_agent_transport.py" not in line
        for line in integration_lines
    )
    # The e2e lane must actually collect the transport test: pin that the
    # pytest invocation targets the whole tests/e2e dir with no --ignore and
    # no -m deselection.
    e2e_pytest_line = e2e_lines[_find_line(e2e_lines, "tests/e2e")]
    assert "--ignore" not in e2e_pytest_line
    assert " -m " not in f" {e2e_pytest_line} "
