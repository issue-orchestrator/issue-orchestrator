"""E2E GitHub utility functions."""

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from issue_orchestrator.adapters.github import resolve_github_token
from issue_orchestrator.entrypoints.setup_wizard_common import detect_repo
from issue_orchestrator.infra.repo_registry import list_repos, registry_file
from tests.fixtures.live_agent_cli import (
    is_claude_authenticated,
    is_claude_available,
)

from .github_client import _github_adapter


def is_gh_authenticated() -> bool:
    """Check if GitHub auth is available."""
    try:
        resolve_github_token(configured_token=None)
        return True
    except Exception:
        return False


class GitHubRateLimitError(Exception):
    """Raised when GitHub API rate limit is exceeded."""
    pass


def check_github_rate_limit(repo: str) -> dict:
    """Check GitHub API rate limit status.

    Returns:
        Dict with 'remaining', 'limit', 'reset_at' keys

    Raises:
        GitHubRateLimitError: If rate limit is exceeded
    """
    try:
        data = _github_adapter(repo).get_rate_limit_snapshot()
        if data is None:
            return {"remaining": -1, "limit": -1, "reset_at": "unknown"}
        # Adapter already returns a dict with core/search/graphql keys
        core = data.get("core", {})
        remaining = core.get("remaining", 0)
        limit = core.get("limit", 5000)
        reset_timestamp = core.get("reset", 0)

        import datetime
        reset_at = datetime.datetime.fromtimestamp(reset_timestamp).strftime("%H:%M:%S") if reset_timestamp else "unknown"

        if remaining == 0:
            raise GitHubRateLimitError(
                f"GitHub API rate limit EXCEEDED!\n"
                f"  Limit: {limit}\n"
                f"  Remaining: {remaining}\n"
                f"  Resets at: {reset_at}\n"
                f"  \n"
                f"  Wait for rate limit to reset or use a different token."
            )

        return {"remaining": remaining, "limit": limit, "reset_at": reset_at}
    except GitHubRateLimitError:
        raise
    except Exception:
        return {"remaining": -1, "limit": -1, "reset_at": "unknown"}


def is_rate_limit_error(error_message: str) -> bool:
    """Check if an error message indicates a rate limit issue."""
    rate_limit_indicators = [
        "rate limit",
        "API rate limit",
        "rate_limit",
        "secondary rate limit",
        "abuse detection",
    ]
    error_lower = error_message.lower()
    return any(indicator.lower() in error_lower for indicator in rate_limit_indicators)


def is_github_connection_error(error_message: str) -> bool:
    """Check if an error message indicates a GitHub connectivity problem."""
    indicators = [
        "error connecting to api.github.com",
        "could not resolve host",
        "network is unreachable",
        "connection timed out",
        "connect: connection refused",
    ]
    error_lower = error_message.lower()
    return any(indicator in error_lower for indicator in indicators)


def is_github_reachable(repo: str) -> bool:
    """Check that GitHub API is reachable for live e2e tests."""
    try:
        snapshot = _github_adapter(repo).get_rate_limit_snapshot()
        return snapshot is not None
    except Exception:
        return False


#: The checkout this harness runs from.
_HARNESS_CHECKOUT = Path(__file__).parent.parent.parent.parent


class E2ETargetRepoError(RuntimeError):
    """The live e2e suite has no safe repository to run against."""


def get_repo_from_git() -> str:
    """The repository this checkout belongs to: its GitHub ``origin`` remote.

    That is the repository the engine developing this code manages, so the
    live suite must never target it. Unreadable means unknown, and unknown is
    refused rather than guessed.
    """
    repo = detect_repo(cwd=_HARNESS_CHECKOUT)
    if repo is None:
        raise E2ETargetRepoError(
            f"Cannot read a GitHub origin remote for this checkout ({_HARNESS_CHECKOUT}), "
            "so the live e2e suite cannot prove E2E_TEST_REPO is not the "
            "production repository."
        )
    return repo


def get_registered_repos() -> dict[str, Path]:
    """The repositories of the checkouts in the operator's io repo registry.

    Keyed by casefolded ``owner/name``, each mapped to the registered checkout
    whose GitHub ``origin`` names it (#9529). An io engine is registered by
    the checkout it runs from (``~/.config/issue-orchestrator/repos.json``),
    so every repository developed there is one an io engine manages. The
    origin, not the YAML an entry last selected, is the identity: an e2e
    engine registers the harness checkout (whose origin is this repository's)
    while naming the test repository only in a temporary config.

    A registered checkout whose repository cannot be read, because it no
    longer exists or has no GitHub origin, is refused rather than skipped:
    the registry records only the path, and an engine can outlive a checkout
    that was deleted, moved or unmounted. The suite's own engines register in
    a registry of their own (``OrchestratorProcess``), never this one.
    """
    repos: dict[str, Path] = {}
    for registered in list_repos():
        checkout = Path(registered.path)
        if not checkout.exists():
            raise _unidentified_registered_checkout(checkout, "no longer exists")
        repo = detect_repo(cwd=checkout)
        if repo is None:
            raise _unidentified_registered_checkout(
                checkout, "has no readable GitHub origin remote"
            )
        repos.setdefault(repo.casefold(), checkout)
    return repos


def _unidentified_registered_checkout(checkout: Path, why: str) -> E2ETargetRepoError:
    return E2ETargetRepoError(
        f"The io repo registry ({registry_file()}) lists {checkout}, which {why}, "
        "so the live e2e suite cannot prove E2E_TEST_REPO is not the repository "
        "an io engine manages from it. Restore the checkout, or, once no io "
        "engine runs from it, remove its entry from the registry."
    )


def resolve_test_repo(
    configured: str | None,
    production_repo: Callable[[], str],
    registered_repos: Callable[[], Mapping[str, Path]],
) -> str:
    """The live suite's target: ``configured``, unless unset or io-managed.

    The suite creates, edits and closes real issues and PRs (#9523), so it
    never falls back to the checkout's own repository: it must be named
    explicitly, and it must be neither that repository nor one any io engine
    on this machine manages (``registered_repos``, #9529). Both readers run
    only once a target is named, so an unset one is reported as such.
    """
    repo = (configured or "").strip()
    if not repo:
        raise E2ETargetRepoError(
            "E2E_TEST_REPO is not set. The live e2e suite creates, edits and "
            "closes real GitHub issues and PRs, so it runs only against a "
            "dedicated test repository: set E2E_TEST_REPO=<owner>/<test-repo>."
        )
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        raise E2ETargetRepoError(f"E2E_TEST_REPO must be 'owner/name', got {repo!r}")
    if repo.casefold() == production_repo().casefold():
        raise E2ETargetRepoError(
            f"E2E_TEST_REPO={repo} is this checkout's own repository, the one "
            "the engine manages. The live e2e suite never runs against it: "
            "point E2E_TEST_REPO at a dedicated test repository."
        )
    checkout = registered_repos().get(repo.casefold())
    if checkout is not None:
        raise E2ETargetRepoError(
            f"E2E_TEST_REPO={repo} is managed by an io engine: the io repo "
            f"registry lists its checkout {checkout}. The live e2e suite never "
            "runs against a managed repository: point E2E_TEST_REPO at a "
            "dedicated test repository."
        )
    return repo


def get_test_repo() -> str:
    """The repository the live e2e suite runs against: ``E2E_TEST_REPO``.

    Required, and never this checkout's own repository nor one an io engine
    manages (see ``resolve_test_repo``). For open-source contributors:
    - Create a test repository (or a fork you do not develop in)
    - Set E2E_TEST_REPO=youruser/yourrepo
    - Run e2e tests against it
    """
    return resolve_test_repo(
        os.environ.get("E2E_TEST_REPO"), get_repo_from_git, get_registered_repos
    )


def env_token_name() -> str | None:
    """Get the name of the environment variable containing the GitHub token."""
    if os.environ.get("GITHUB_TOKEN"):
        return "GITHUB_TOKEN"
    if os.environ.get("GH_TOKEN"):
        return "GH_TOKEN"
    return None
