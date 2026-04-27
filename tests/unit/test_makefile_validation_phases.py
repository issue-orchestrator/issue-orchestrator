"""Tests for Makefile validation phase orchestration."""

import os
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


def _find_line(lines: list[str], *fragments: str) -> int:
    for index, line in enumerate(lines):
        if all(fragment in line for fragment in fragments):
            return index
    raise AssertionError(f"Missing line containing {fragments!r}. Output:\n" + "\n".join(lines))


def test_validate_impl_runs_core_phases_with_separate_job_caps():
    lines = _dry_run("_validate-impl")

    static_index = _find_line(lines, "-j10", "_validate-static-impl")
    core_tests_index = _find_line(lines, "-j1", "_validate-core-tests-impl")
    web_index = _find_line(lines, "-j1", "test-web")

    assert static_index < core_tests_index < web_index


def test_validate_pr_impl_runs_agent_phase_after_validate_phase():
    lines = _dry_run("_validate-pr-impl")

    validate_index = _find_line(lines, "_validate-impl")
    agent_index = _find_line(lines, "-j1", "_validate-agent-impl")

    assert validate_index < agent_index


def test_validate_full_impl_runs_e2e_after_pr_phase():
    lines = _dry_run("_validate-full-impl")

    pr_index = _find_line(lines, "_validate-pr-impl")
    e2e_index = _find_line(lines, "-j1", "test-e2e")

    assert pr_index < e2e_index


def test_validate_pr_raw_does_not_schedule_entire_graph_at_validate_jobs():
    lines = _dry_run("validate-pr-raw")
    raw_pr_lines = [line for line in lines if "_validate-pr-impl" in line]

    assert raw_pr_lines
    assert all("-j10" not in line for line in raw_pr_lines)
