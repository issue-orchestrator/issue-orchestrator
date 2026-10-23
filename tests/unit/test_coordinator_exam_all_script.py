"""exam_all.sh runs the live exam opted in and against its dedicated repository (#9537).

The live suite is collected only on IO_E2E=1 and targets only E2E_TEST_REPO
(#9523), and the exam cases skip without E2E_TECH_LEAD_EXAM=1, so the script
must hand all three to every pytest it starts. Each test runs the real script
against a stub pytest that records the environment it was given.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.process_group_run import run_in_process_group

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "coordinator" / "exam_all.sh"
EXAM_REPO = "issue-orchestrator/io-e2e-sandbox"

_FAKE_PYTEST = """#!/usr/bin/env bash
printf '%s\\t%s\\t%s\\n' "${IO_E2E-<unset>}" "${E2E_TECH_LEAD_EXAM-<unset>}" \\
  "${E2E_TEST_REPO-<unset>}" >> "$FAKE_PYTEST_LOG"
echo "==== 1 passed in 0.01s ===="
"""


class _ExamRunner:
    """A HOME holding the exam-runner checkout and COORD_DIR the script expects."""

    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.coord = root / "coord"
        self.pytest_log = root / "pytest-env.log"
        fake = (
            self.home / "dev/worktree/issue-orchestrator/exam-runner/.venv/bin/pytest"
        )
        fake.parent.mkdir(parents=True)
        fake.write_text(_FAKE_PYTEST, encoding="utf-8")
        fake.chmod(0o755)
        (self.coord / "exam").mkdir(parents=True)

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        # A clean environment, so neither this session's opt-in state nor its
        # E2E_TEST_REPO decides the outcome.
        base = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "COORD_DIR": str(self.coord),
            "FAKE_PYTEST_LOG": str(self.pytest_log),
        }
        # In its own process group: a script stuck on the lock leaves a sleep
        # behind, and the timeout must take that down with it.
        return run_in_process_group(
            ["bash", str(SCRIPT), "abc1234"], env={**base, **env}, timeout=30
        )

    def pytest_envs(self) -> list[tuple[str, str, str]]:
        """(IO_E2E, E2E_TECH_LEAD_EXAM, E2E_TEST_REPO) for each pytest started."""
        if not self.pytest_log.exists():
            return []
        envs = []
        for line in self.pytest_log.read_text(encoding="utf-8").splitlines():
            live_opt_in, exam_opt_in, repo = line.split("\t")
            envs.append((live_opt_in, exam_opt_in, repo))
        return envs


@pytest.fixture
def runner(tmp_path: Path) -> _ExamRunner:
    return _ExamRunner(tmp_path)


def _assert_every_case_ran_with(
    runner: _ExamRunner, result: subprocess.CompletedProcess[str], repo: str
) -> None:
    assert result.returncode == 0, result.stderr
    cases = result.stdout.splitlines()
    envs = runner.pytest_envs()
    assert cases, "the script ran no exam case"
    assert len(envs) == len(cases)
    assert set(envs) == {("1", "1", repo)}


def test_an_unset_target_runs_every_case_on_the_dedicated_exam_repository(runner):
    _assert_every_case_ran_with(runner, runner.run(), EXAM_REPO)


def test_a_named_target_is_passed_to_every_case(runner):
    result = runner.run(E2E_TEST_REPO="someone/other-sandbox")

    _assert_every_case_ran_with(runner, result, "someone/other-sandbox")


@pytest.mark.parametrize("caller_opt_in", ["0", ""])
def test_the_live_suite_opt_in_is_set_whatever_the_caller_exported(
    runner, caller_opt_in
):
    result = runner.run(IO_E2E=caller_opt_in, E2E_TEST_REPO="someone/other-sandbox")

    _assert_every_case_ran_with(runner, result, "someone/other-sandbox")


@pytest.mark.parametrize("configured", ["", "   "])
def test_an_empty_target_is_refused_before_the_lock_not_defaulted(runner, configured):
    # Another exam holds the lock: a script that got as far as waiting on it
    # would sleep past the timeout instead of refusing.
    (runner.coord / "exam.lock").mkdir()

    result = runner.run(E2E_TEST_REPO=configured)

    assert result.returncode != 0
    assert "E2E_TEST_REPO" in result.stderr
    assert runner.pytest_envs() == []
    assert (runner.coord / "exam.lock").is_dir()
