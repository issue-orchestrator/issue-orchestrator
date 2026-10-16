"""The live e2e suite's repository is named explicitly and is never one io
manages: not production (#9523), nor any repository in the io repo registry
(#9529)."""

from pathlib import Path

import pytest

from issue_orchestrator.infra.repo_registry import add_repo
from tests.e2e.fixtures import github_utils
from tests.e2e.fixtures.github_utils import (
    E2ETargetRepoError,
    get_test_repo,
    resolve_test_repo,
)

PRODUCTION = "issue-orchestrator/issue-orchestrator"
MANAGED = "porchpin/porchpin"


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch, tmp_path):
    """Every test reads its own io repo registry, never the operator's."""
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_CONFIG_DIR", str(tmp_path / "io-config"))


def _production() -> str:
    return PRODUCTION


def _never_read() -> str:
    raise AssertionError("an unset target must be refused before origin is read")


def _never_read_registry() -> dict[str, Path]:
    raise AssertionError("an unset target must be refused before the registry is read")


def _nothing_registered() -> dict[str, Path]:
    return {}


def _registered() -> dict[str, Path]:
    return {MANAGED: Path("/home/operator/dev/porchpin")}


def _register_checkout(tmp_path: Path, name: str) -> Path:
    checkout = tmp_path / name
    checkout.mkdir()
    add_repo(checkout)
    return checkout.resolve()


def _origins(monkeypatch, by_checkout: dict[Path, str | None]) -> None:
    """``origin`` of each registered checkout; the harness checkout is production."""
    monkeypatch.setattr(
        github_utils, "detect_repo", lambda cwd: by_checkout.get(cwd, PRODUCTION)
    )


@pytest.mark.parametrize("configured", [None, "", "   "])
def test_an_unset_target_is_refused_not_guessed(configured):
    with pytest.raises(E2ETargetRepoError, match="E2E_TEST_REPO is not set"):
        resolve_test_repo(configured, _never_read, _never_read_registry)


@pytest.mark.parametrize(
    "configured", [PRODUCTION, "Issue-Orchestrator/Issue-Orchestrator", f" {PRODUCTION} "]
)
def test_the_checkouts_own_repository_is_refused(configured):
    with pytest.raises(E2ETargetRepoError, match="this checkout's own repository"):
        resolve_test_repo(configured, _production, _nothing_registered)


@pytest.mark.parametrize("configured", ["issue-orchestrator", "a/b/c", "/repo", "owner/"])
def test_a_malformed_target_is_refused(configured):
    with pytest.raises(E2ETargetRepoError, match="must be 'owner/name'"):
        resolve_test_repo(configured, _production, _nothing_registered)


@pytest.mark.parametrize("configured", [MANAGED, "PorchPin/PorchPin", f" {MANAGED} "])
def test_a_repository_another_io_engine_manages_is_refused(configured):
    with pytest.raises(E2ETargetRepoError, match="managed by an io engine") as refusal:
        resolve_test_repo(configured, _production, _registered)

    assert "/home/operator/dev/porchpin" in str(refusal.value)


def test_a_dedicated_test_repository_is_the_target():
    assert (
        resolve_test_repo(" someone/io-e2e-sandbox ", _production, _registered)
        == "someone/io-e2e-sandbox"
    )


def test_the_git_remote_is_never_a_fallback(monkeypatch):
    monkeypatch.delenv("E2E_TEST_REPO", raising=False)
    _origins(monkeypatch, {})

    with pytest.raises(E2ETargetRepoError, match="E2E_TEST_REPO is not set"):
        get_test_repo()


def test_e2e_test_repo_naming_the_checkouts_origin_is_refused(monkeypatch):
    monkeypatch.setenv("E2E_TEST_REPO", PRODUCTION)
    _origins(monkeypatch, {})

    with pytest.raises(E2ETargetRepoError, match="this checkout's own repository"):
        get_test_repo()


def test_e2e_test_repo_naming_a_registered_checkouts_repository_is_refused(
    monkeypatch, tmp_path
):
    checkout = _register_checkout(tmp_path, "porchpin")
    _origins(monkeypatch, {checkout: MANAGED})
    monkeypatch.setenv("E2E_TEST_REPO", MANAGED)

    with pytest.raises(E2ETargetRepoError, match="managed by an io engine"):
        get_test_repo()


def test_a_deleted_registered_checkout_manages_nothing(monkeypatch, tmp_path):
    checkout = _register_checkout(tmp_path, "porchpin")
    checkout.rmdir()
    _origins(monkeypatch, {checkout: MANAGED})
    monkeypatch.setenv("E2E_TEST_REPO", MANAGED)

    assert get_test_repo() == MANAGED


def test_a_registered_checkout_with_an_unreadable_origin_is_refused(monkeypatch, tmp_path):
    checkout = _register_checkout(tmp_path, "local-only")
    _origins(monkeypatch, {checkout: None})
    monkeypatch.setenv("E2E_TEST_REPO", "someone/io-e2e-sandbox")

    with pytest.raises(E2ETargetRepoError, match="cannot prove") as refusal:
        get_test_repo()

    assert str(checkout) in str(refusal.value)


def test_an_unreadable_origin_is_refused_rather_than_trusted(monkeypatch):
    monkeypatch.setenv("E2E_TEST_REPO", "someone/io-e2e-sandbox")
    monkeypatch.setattr(github_utils, "detect_repo", lambda cwd: None)

    with pytest.raises(E2ETargetRepoError, match="cannot prove"):
        get_test_repo()


def test_e2e_test_repo_is_the_target(monkeypatch, tmp_path):
    checkout = _register_checkout(tmp_path, "porchpin")
    _origins(monkeypatch, {checkout: MANAGED})
    monkeypatch.setenv("E2E_TEST_REPO", "someone/io-e2e-sandbox")

    assert get_test_repo() == "someone/io-e2e-sandbox"
