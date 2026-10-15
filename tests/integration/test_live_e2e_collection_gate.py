"""Real pytest runs never reach the live e2e suite implicitly (#9523).

On 2026-10-14 an agent's ``pytest -q $(ls ...)`` lost every path to an
unmatched zsh glob, pytest fell back to ``testpaths`` and ``tests/e2e``'s
cleanup closed 17 real PRs. These runs go through this checkout's real root
conftest, so its wiring of ``tests/live_e2e_gate.py`` is what is under test.

Every run is ``--collect-only``: if the gate failed, nothing would reach
GitHub, the run would only list what it would have run.
"""

import os
import subprocess
import sys
from pathlib import Path

from tests.live_e2e_gate import AGENT_SESSION_ENVS, OPT_IN_ENV

from .conftest import xdist_timeout

ROOT = Path(__file__).resolve().parents[2]

#: The incident's shape, made small: no paths, so pytest falls back to
#: ``testpaths``, walked only through ``tests/e*``. ``tests/e2e_web`` stays in
#: the walk as the control: collected, it shows the walk reached the live
#: suite's siblings, so the live suite's absence is the gate's doing.
EMPTY_PATH_LIST = ("-o", "testpaths=tests", f"--ignore-glob={ROOT}/tests/[!e]*")


def _collect(*args: str, **environ: str) -> subprocess.CompletedProcess[str]:
    scrubbed = {*AGENT_SESSION_ENVS, OPT_IN_ENV, "E2E_TEST_REPO", "PYTEST_ADDOPTS"}
    environment = {
        name: value for name, value in os.environ.items() if name not in scrubbed
    }
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
        cwd=ROOT,
        env={**environment, **environ},
        capture_output=True,
        text=True,
        timeout=xdist_timeout(90),
    )


def _collected_live_suite(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.startswith("tests/e2e/")]


def test_an_empty_path_list_collects_everything_but_the_live_suite():
    result = _collect(*EMPTY_PATH_LIST)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/e2e_web/" in result.stdout
    assert _collected_live_suite(result) == []


def test_an_agent_session_never_satisfies_the_opt_in():
    result = _collect(
        *EMPTY_PATH_LIST, IO_E2E="1", ISSUE_ORCHESTRATOR_SESSION_ID="coding-1"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/e2e_web/" in result.stdout
    assert _collected_live_suite(result) == []


def test_naming_the_live_suite_without_the_opt_in_is_a_usage_error():
    result = _collect("tests/unit/test_e2e_flows.py", "tests/e2e/test_live_orchestrator.py")

    assert result.returncode == 4, result.stdout + result.stderr
    assert "It runs only on explicit opt-in: set IO_E2E=1" in result.stderr


def test_an_agent_session_naming_the_live_suite_is_refused_even_opted_in():
    result = _collect("tests/e2e", IO_E2E="1", ORCHESTRATOR_SESSION_ID="coding-1")

    assert result.returncode == 4, result.stdout + result.stderr
    assert "Agent sessions may never run it (ORCHESTRATOR_SESSION_ID is set)" in result.stderr


def test_the_opted_in_suite_refuses_to_start_without_a_test_repository():
    result = _collect("tests/e2e/test_live_orchestrator.py", IO_E2E="1")

    assert result.returncode == 4, result.stdout + result.stderr
    assert "E2E_TEST_REPO is not set" in result.stderr


#: Not this checkout's origin, so the suite's repository check passes and only
#: the opt-in is left to refuse.
_NON_PRODUCTION_REPO = "example-owner/e2e-test-repo"


def test_the_suite_refuses_without_the_opt_in_even_without_the_root_conftest():
    """``--confcutdir=tests/e2e`` skips the root conftest and its collection
    guard with it; the suite's own conftest must still refuse (#9523 review F1)."""
    result = _collect("--confcutdir=tests/e2e", "tests/e2e", E2E_TEST_REPO=_NON_PRODUCTION_REPO)

    assert result.returncode == 4, result.stdout + result.stderr
    assert "It runs only on explicit opt-in: set IO_E2E=1" in result.stderr
    assert _collected_live_suite(result) == []


def test_an_agent_session_is_refused_by_the_suite_even_without_the_root_conftest():
    result = _collect(
        "--confcutdir=tests/e2e",
        "tests/e2e",
        E2E_TEST_REPO=_NON_PRODUCTION_REPO,
        IO_E2E="1",
        ISSUE_ORCHESTRATOR_SESSION_ID="coding-1",
    )

    assert result.returncode == 4, result.stdout + result.stderr
    assert "Agent sessions may never run it (ISSUE_ORCHESTRATOR_SESSION_ID is set)" in result.stderr
    assert _collected_live_suite(result) == []
