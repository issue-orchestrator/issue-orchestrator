"""The live e2e suite's repository is named explicitly and is never production (#9523)."""

import pytest

from tests.e2e.fixtures import github_utils
from tests.e2e.fixtures.github_utils import (
    E2ETargetRepoError,
    get_test_repo,
    resolve_test_repo,
)

PRODUCTION = "issue-orchestrator/issue-orchestrator"


def _production() -> str:
    return PRODUCTION


def _never_read() -> str:
    raise AssertionError("an unset target must be refused before origin is read")


@pytest.mark.parametrize("configured", [None, "", "   "])
def test_an_unset_target_is_refused_not_guessed(configured):
    with pytest.raises(E2ETargetRepoError, match="E2E_TEST_REPO is not set"):
        resolve_test_repo(configured, _never_read)


@pytest.mark.parametrize(
    "configured", [PRODUCTION, "Issue-Orchestrator/Issue-Orchestrator", f" {PRODUCTION} "]
)
def test_the_checkouts_own_repository_is_refused(configured):
    with pytest.raises(E2ETargetRepoError, match="this checkout's own repository"):
        resolve_test_repo(configured, _production)


@pytest.mark.parametrize("configured", ["issue-orchestrator", "a/b/c", "/repo", "owner/"])
def test_a_malformed_target_is_refused(configured):
    with pytest.raises(E2ETargetRepoError, match="must be 'owner/name'"):
        resolve_test_repo(configured, _production)


def test_a_dedicated_test_repository_is_the_target():
    assert resolve_test_repo(" someone/io-e2e-sandbox ", _production) == "someone/io-e2e-sandbox"


def test_the_git_remote_is_never_a_fallback(monkeypatch):
    monkeypatch.delenv("E2E_TEST_REPO", raising=False)
    monkeypatch.setattr(github_utils, "detect_repo", lambda cwd: PRODUCTION)

    with pytest.raises(E2ETargetRepoError, match="E2E_TEST_REPO is not set"):
        get_test_repo()


def test_e2e_test_repo_naming_the_checkouts_origin_is_refused(monkeypatch):
    monkeypatch.setenv("E2E_TEST_REPO", PRODUCTION)
    monkeypatch.setattr(github_utils, "detect_repo", lambda cwd: PRODUCTION)

    with pytest.raises(E2ETargetRepoError, match="this checkout's own repository"):
        get_test_repo()


def test_an_unreadable_origin_is_refused_rather_than_trusted(monkeypatch):
    monkeypatch.setenv("E2E_TEST_REPO", "someone/io-e2e-sandbox")
    monkeypatch.setattr(github_utils, "detect_repo", lambda cwd: None)

    with pytest.raises(E2ETargetRepoError, match="cannot prove"):
        get_test_repo()


def test_e2e_test_repo_is_the_target(monkeypatch):
    monkeypatch.setenv("E2E_TEST_REPO", "someone/io-e2e-sandbox")
    monkeypatch.setattr(github_utils, "detect_repo", lambda cwd: PRODUCTION)

    assert get_test_repo() == "someone/io-e2e-sandbox"
