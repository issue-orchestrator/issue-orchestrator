"""Tests for the issue-list fetch resilience policy owner.

These verify the classification policy that decides whether a repository-host
fetch failure should degrade-and-stay-up (transient) or fail-fast with an
actionable message (permanent), and that a *persistent* repo-not-found is
promoted from transient to permanent.
"""

import pytest

from issue_orchestrator.adapters.github.errors import GitHubTransportError
from issue_orchestrator.adapters.github.http_client import GitHubHttpError
from issue_orchestrator.control.issue_fetch_resilience import (
    FetchFailureKind,
    IssueFetchResilience,
    PermanentIssueFetchError,
    TransientIssueFetchError,
)


def http_error(status_code: int, *, response_text: str = "") -> GitHubHttpError:
    return GitHubHttpError(
        f"GitHub request failed: {status_code}",
        status_code=status_code,
        response_text=response_text,
    )


class TestClassification:
    """record_failure() classifies a single failure (no call-site coupling)."""

    def test_single_404_is_transient(self) -> None:
        resilience = IssueFetchResilience("owner/repo")
        verdict = resilience.record_failure(http_error(404))
        assert verdict.kind is FetchFailureKind.TRANSIENT
        assert not verdict.is_permanent

    def test_persistent_404_promotes_to_permanent(self) -> None:
        resilience = IssueFetchResilience("owner/repo", repo_not_found_tolerance=3)
        assert not resilience.record_failure(http_error(404)).is_permanent
        assert not resilience.record_failure(http_error(404)).is_permanent
        verdict = resilience.record_failure(http_error(404))
        assert verdict.is_permanent
        # Actionable message names the repo and the config/token cause.
        assert "owner/repo" in verdict.summary
        assert "repo.name" in verdict.suggested_fix

    def test_success_resets_repo_not_found_streak(self) -> None:
        resilience = IssueFetchResilience("owner/repo", repo_not_found_tolerance=2)
        resilience.record_failure(http_error(404))  # streak = 1
        resilience.note_success()  # streak reset
        # First 404 after success is transient again (would be permanent at 2
        # only if the streak had not reset).
        verdict = resilience.record_failure(http_error(404))
        assert verdict.kind is FetchFailureKind.TRANSIENT

    def test_401_is_permanent_immediately(self) -> None:
        resilience = IssueFetchResilience("owner/repo")
        verdict = resilience.record_failure(http_error(401))
        assert verdict.is_permanent
        assert "owner/repo" in verdict.summary

    def test_403_forbidden_is_permanent(self) -> None:
        resilience = IssueFetchResilience("owner/repo")
        verdict = resilience.record_failure(
            http_error(403, response_text="Resource not accessible by integration")
        )
        assert verdict.is_permanent

    def test_403_rate_limit_is_transient(self) -> None:
        resilience = IssueFetchResilience("owner/repo")
        verdict = resilience.record_failure(
            http_error(403, response_text="You have exceeded a secondary rate limit")
        )
        assert verdict.kind is FetchFailureKind.TRANSIENT

    def test_429_is_transient(self) -> None:
        resilience = IssueFetchResilience("owner/repo")
        verdict = resilience.record_failure(http_error(429))
        assert verdict.kind is FetchFailureKind.TRANSIENT

    def test_5xx_is_transient(self) -> None:
        resilience = IssueFetchResilience("owner/repo")
        verdict = resilience.record_failure(http_error(503))
        assert verdict.kind is FetchFailureKind.TRANSIENT

    def test_5xx_never_promotes_to_permanent(self) -> None:
        # A sustained GitHub 5xx outage is genuinely recoverable; it must keep
        # riding it out and never auto-fail-fast (only repo-not-found does).
        resilience = IssueFetchResilience("owner/repo", repo_not_found_tolerance=2)
        for _ in range(10):
            assert not resilience.record_failure(http_error(503)).is_permanent

    def test_transport_error_is_transient(self) -> None:
        resilience = IssueFetchResilience("owner/repo")
        verdict = resilience.record_failure(
            GitHubTransportError("connection reset", original=OSError("reset"))
        )
        assert verdict.kind is FetchFailureKind.TRANSIENT


class TestGuard:
    """guard() runs a fetch under the policy and raises typed outcomes."""

    def test_returns_value_and_resets_on_success(self) -> None:
        resilience = IssueFetchResilience("owner/repo", repo_not_found_tolerance=2)
        resilience.record_failure(http_error(404))  # streak = 1

        result = resilience.guard(lambda: [1, 2, 3])

        assert result == [1, 2, 3]
        # Success reset the streak: a fresh 404 is transient, not permanent.
        assert resilience.record_failure(http_error(404)).kind is FetchFailureKind.TRANSIENT

    def test_raises_transient_for_recoverable_failure(self) -> None:
        resilience = IssueFetchResilience("owner/repo")

        def boom() -> list[int]:
            raise http_error(503)

        with pytest.raises(TransientIssueFetchError) as exc_info:
            resilience.guard(boom)
        assert exc_info.value.verdict.kind is FetchFailureKind.TRANSIENT
        assert exc_info.value.suggested_fix

    def test_raises_permanent_for_auth_failure(self) -> None:
        resilience = IssueFetchResilience("owner/repo")

        def boom() -> list[int]:
            raise http_error(401)

        with pytest.raises(PermanentIssueFetchError) as exc_info:
            resilience.guard(boom)
        assert exc_info.value.verdict.is_permanent

    def test_does_not_swallow_non_repository_errors(self) -> None:
        # Real bugs (not repository-host failures) must surface, not be masked
        # by the resilience policy.
        resilience = IssueFetchResilience("owner/repo")

        def boom() -> list[int]:
            raise ValueError("a real bug")

        with pytest.raises(ValueError, match="a real bug"):
            resilience.guard(boom)
