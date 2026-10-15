"""The live e2e gate's decisions (#9523).

``tests/integration/test_live_e2e_collection_gate.py`` drives real pytest runs
through the root conftest's wiring of these decisions.
"""

import pytest

from tests.live_e2e_gate import LIVE_E2E_DIR, LiveE2EGate, is_live_e2e_target

ROOT = LIVE_E2E_DIR.parent.parent
CLOSED = LiveE2EGate.from_environ({})
OPTED_IN = LiveE2EGate.from_environ({"IO_E2E": "1"})
AGENT_OPTED_IN = LiveE2EGate.from_environ(
    {"IO_E2E": "1", "ISSUE_ORCHESTRATOR_SESSION_ID": "coding-1"}
)


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes"])
def test_only_io_e2e_equal_to_1_opts_in(value):
    environ = {} if value is None else {"IO_E2E": value}
    assert not LiveE2EGate.from_environ(environ).allows_live_e2e


def test_the_opt_in_allows_the_live_suite_outside_an_agent_session():
    assert OPTED_IN.allows_live_e2e


@pytest.mark.parametrize(
    "session_env", ["ISSUE_ORCHESTRATOR_SESSION_ID", "ORCHESTRATOR_SESSION_ID"]
)
def test_an_agent_session_never_satisfies_the_opt_in(session_env):
    gate = LiveE2EGate.from_environ({"IO_E2E": "1", session_env: "coding-1"})

    assert not gate.allows_live_e2e
    assert gate.ignores(LIVE_E2E_DIR)


def test_implicit_collection_skips_the_live_suite_and_nothing_else():
    assert CLOSED.ignores(LIVE_E2E_DIR)
    assert CLOSED.ignores(LIVE_E2E_DIR / "test_live_orchestrator.py")
    assert not CLOSED.ignores(ROOT / "tests")
    assert not CLOSED.ignores(ROOT / "tests" / "e2e_web")
    assert not CLOSED.ignores(ROOT / "tests" / "unit" / "test_e2e_flows.py")


def test_the_opt_in_lets_collection_into_the_live_suite():
    assert not OPTED_IN.ignores(LIVE_E2E_DIR)


@pytest.mark.parametrize(
    "target",
    [
        "tests/e2e",
        "tests/e2e/",
        "tests/e2e/test_live_orchestrator.py",
        "tests/e2e/test_live_orchestrator.py::TestLabelDetection::test_blocked_label_detection",
        "tests/unit/../e2e",
        str(LIVE_E2E_DIR),
    ],
)
def test_targets_that_name_the_live_suite(target):
    assert is_live_e2e_target(target, ROOT)


@pytest.mark.parametrize(
    "target",
    [
        "tests",
        "tests/unit",
        "tests/e2e_web",
        "tests/unit/test_e2e_flows.py::test_flow_cleanup_closes_prs_before_issues",
        "packages/agent_runner/tests",
    ],
)
def test_targets_outside_the_live_suite(target):
    assert not is_live_e2e_target(target, ROOT)


def test_targets_are_relative_to_the_invocation_directory():
    assert is_live_e2e_target("e2e/test_live_orchestrator.py", ROOT / "tests")


def test_the_testpaths_fallback_is_never_refused():
    """A bare ``pytest`` gets ``testpaths``; it is narrowed, not refused."""
    assert CLOSED.refusal(["tests", "packages/agent_runner/tests"], ROOT) is None
    assert AGENT_OPTED_IN.refusal(["tests", "packages/agent_runner/tests"], ROOT) is None


def test_naming_the_live_suite_without_the_opt_in_names_the_opt_in():
    refusal = CLOSED.refusal(["tests/unit", "tests/e2e/test_live_orchestrator.py"], ROOT)

    assert refusal is not None
    assert "IO_E2E=1" in refusal
    assert "E2E_TEST_REPO" in refusal
    assert refusal.endswith("Refused: tests/e2e/test_live_orchestrator.py")


def test_an_agent_session_naming_the_live_suite_is_refused_even_opted_in():
    refusal = AGENT_OPTED_IN.refusal(["tests/e2e"], ROOT)

    assert refusal is not None
    assert "Agent sessions may never run it" in refusal
    assert "ISSUE_ORCHESTRATOR_SESSION_ID" in refusal


def test_the_opted_in_suite_may_be_named():
    assert OPTED_IN.refusal(["tests/e2e"], ROOT) is None
