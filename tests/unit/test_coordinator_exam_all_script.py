"""exam_all.sh runs every exam case, opted in and against its dedicated repository.

The live suite is collected only on IO_E2E=1 and targets only E2E_TEST_REPO
(#9523), and the exam cases skip without E2E_TECH_LEAD_EXAM=1, so the script
must hand all three to every pytest it starts (#9537). It runs the cases pytest
collects from the exam, so a defined case is never left out (#9542). Each test
runs the real script against a stub pytest that answers the collection with
the given output and records every invocation.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.testing.exam.cases import EXAM_CASE_IDS

from tests.process_group_run import run_in_process_group

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "coordinator" / "exam_all.sh"
EXAM_REPO = "issue-orchestrator/io-e2e-sandbox"
REF = "abc1234"

_FAKE_PYTEST = """#!/usr/bin/env bash
mode=run
for arg in "$@"; do [[ $arg == --collect-only ]] && mode=collect; done
printf '%s\\t%s\\t%s\\t%s\\t%s\\n' "$mode" "${IO_E2E-<unset>}" \\
  "${E2E_TECH_LEAD_EXAM-<unset>}" "${E2E_TEST_REPO-<unset>}" "$*" >> "$FAKE_PYTEST_LOG"
if [[ $mode == collect ]]; then
  printf '%s' "$FAKE_COLLECT_OUTPUT"
  exit "$FAKE_COLLECT_RC"
fi
echo "==== 1 passed in 0.01s ===="
"""

_NODE = "tests/e2e/test_tech_lead_exam.py::test_tech_lead_exam[case-{}]"


def _collection(case_ids: tuple[str, ...]) -> str:
    """``pytest --collect-only -q`` output for the exam's cases, with the live
    log line and warnings summary a real collection prints around them."""
    nodes = [_NODE.format(case_id) for case_id in case_ids]
    return "\n".join(
        [
            "[E2E] Fail-fast enabled (maxfail=1)",
            *nodes,
            "",
            "=============================== warnings summary ===============================",
            *nodes[:1],
            "  conftest.py:1: DeprecationWarning: deprecated",
            "",
            f"{len(nodes)} tests collected in 0.42s",
            "",
        ]
    )


class _Invocation:
    def __init__(self, line: str) -> None:
        self.mode, live_opt_in, exam_opt_in, repo, self.args = line.split("\t")
        self.env = (live_opt_in, exam_opt_in, repo)

    @property
    def case_id(self) -> str:
        match = re.search(r"::test_tech_lead_exam\[case-([^\]]+)\]", self.args)
        assert match, f"a case run that selects no case: {self.args}"
        return match.group(1)


class _ExamRunner:
    """A HOME holding the exam-runner checkout and COORD_DIR the script expects."""

    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.coord = root / "coord"
        self.pytest_log = root / "pytest-calls.log"
        fake = (
            self.home / "dev/worktree/issue-orchestrator/exam-runner/.venv/bin/pytest"
        )
        fake.parent.mkdir(parents=True)
        fake.write_text(_FAKE_PYTEST, encoding="utf-8")
        fake.chmod(0o755)
        (self.coord / "exam").mkdir(parents=True)

    def run(
        self,
        *,
        collected: str = _collection(EXAM_CASE_IDS),
        collect_rc: int = 0,
        **env: str,
    ) -> subprocess.CompletedProcess[str]:
        # A clean environment, so neither this session's opt-in state nor its
        # E2E_TEST_REPO decides the outcome.
        base = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "COORD_DIR": str(self.coord),
            "FAKE_PYTEST_LOG": str(self.pytest_log),
            "FAKE_COLLECT_OUTPUT": collected,
            "FAKE_COLLECT_RC": str(collect_rc),
        }
        # In its own process group: a script stuck on the lock leaves a sleep
        # behind, and the timeout must take that down with it.
        return run_in_process_group(
            ["bash", str(SCRIPT), REF], env={**base, **env}, timeout=30
        )

    def invocations(self) -> list[_Invocation]:
        if not self.pytest_log.exists():
            return []
        lines = self.pytest_log.read_text(encoding="utf-8").splitlines()
        return [_Invocation(line) for line in lines]

    def collections(self) -> list[_Invocation]:
        return [call for call in self.invocations() if call.mode == "collect"]

    def case_runs(self) -> list[_Invocation]:
        return [call for call in self.invocations() if call.mode == "run"]


@pytest.fixture
def runner(tmp_path: Path) -> _ExamRunner:
    return _ExamRunner(tmp_path)


def _assert_every_case_ran_with(
    runner: _ExamRunner, result: subprocess.CompletedProcess[str], repo: str
) -> None:
    assert result.returncode == 0, result.stderr
    cases = result.stdout.splitlines()
    runs = runner.case_runs()
    assert cases, "the script ran no exam case"
    assert len(runs) == len(cases)
    assert {call.env for call in runner.invocations()} == {("1", "1", repo)}


def test_every_case_the_exam_defines_is_run_once(runner):
    result = runner.run()

    assert result.returncode == 0, result.stderr
    ran = [call.case_id for call in runner.case_runs()]
    assert sorted(ran) == sorted(EXAM_CASE_IDS)
    assert len(runner.collections()) == 1
    # One summary line and one log per case, named by its letter, as before.
    letters = sorted(case_id.split("-", 1)[0] for case_id in EXAM_CASE_IDS)
    summary = sorted(line.split(" ", 1)[0] for line in result.stdout.splitlines())
    assert summary == letters
    logs = sorted(log.name for log in (runner.coord / "exam").glob(f"*-{REF}.log"))
    assert logs == sorted([f"collect-{REF}.log", *(f"{c}-{REF}.log" for c in letters)])


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


@pytest.mark.parametrize(
    ("collected", "collect_rc"),
    [
        pytest.param(
            "ERROR: tests/e2e is the live suite; set IO_E2E=1\n",
            4,
            id="collection-refused",
        ),
        pytest.param(
            "\nno tests collected (12 deselected) in 0.10s\n",
            5,
            id="every-case-deselected",
        ),
        pytest.param(
            "tests/e2e/test_tech_lead_exam.py::test_renamed[case-A]\n\n1 test collected in 0.1s\n",
            0,
            id="no-exam-case-collected",
        ),
    ],
)
def test_a_collection_with_no_case_fails_without_running_any(
    runner, collected, collect_rc
):
    result = runner.run(collected=collected, collect_rc=collect_rc)

    assert result.returncode != 0
    assert "no case to run" in result.stderr
    assert collected.strip().splitlines()[-1] in result.stderr
    assert runner.case_runs() == []
    assert result.stdout == ""
    assert not (runner.coord / "exam.lock").exists()


@pytest.mark.parametrize("configured", ["", "   "])
def test_an_empty_target_is_refused_before_the_lock_not_defaulted(runner, configured):
    # Another exam holds the lock: a script that got as far as waiting on it
    # would sleep past the timeout instead of refusing.
    (runner.coord / "exam.lock").mkdir()

    result = runner.run(E2E_TEST_REPO=configured)

    assert result.returncode != 0
    assert "E2E_TEST_REPO" in result.stderr
    assert runner.invocations() == []
    assert (runner.coord / "exam.lock").is_dir()
