"""Resilience policy for issue-list fetches against the repository host.

A single ``GitHubHttpError`` on the issue-list call used to take the entire
orchestrator process down, regardless of whether the failure was permanent
(a misconfigured/renamed/deleted repo, a revoked token) or transient (a brief
GitHub blip, eventual-consistency right after a rename, a 5xx). For a daemon
that is otherwise designed to be crash-safe and recover from labels, dying on a
recoverable blip is the wrong default.

This module is the *single owner* of the policy that decides, for a given
repository-host failure, whether the orchestrator should:

- **degrade and stay up** (transient — skip this fetch, keep the cached queue,
  recover on the next cycle), or
- **fail fast with a clear, actionable message** (permanent — auth failure, or
  a repo-not-found that has persisted across several consecutive attempts).

The two active fetch boundaries (startup and the steady-state tick) consult one
shared instance so the consecutive-failure count spans startup *and* ticks, and
so the policy is enforced in one place instead of being duplicated and
diverging across call sites.

Classification is done purely from port-level attributes on
``RepositoryHostError`` (``status_code`` / ``response_text``); this module does
not import any adapter, keeping the control/adapter boundary intact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable, TypeVar

from ..ports.repository_host import RepositoryHostError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# A repo-not-found (404) is treated as transient until it has persisted for this
# many consecutive attempts, at which point it is almost certainly a genuine
# misconfiguration (wrong ``repo.name``, deleted/renamed repo, revoked access)
# and is promoted to a permanent, fail-fast condition.
DEFAULT_REPO_NOT_FOUND_TOLERANCE = 5


class FetchFailureKind(Enum):
    """How a repository-host fetch failure should be handled."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"


@dataclass(frozen=True)
class FetchFailureVerdict:
    """The owner's decision about a single repository-host fetch failure.

    Attributes:
        kind: TRANSIENT (degrade & stay up) or PERMANENT (fail fast).
        summary: Short, human-readable description for logs.
        suggested_fix: Actionable next step (repo name + token-scope hint for
            permanent failures).
        consecutive_repo_not_found: How many consecutive 404s have been seen.
            Resets on any successful fetch *and* on any non-404 transient
            failure, so only back-to-back 404s count toward promotion.
    """

    kind: FetchFailureKind
    summary: str
    suggested_fix: str
    consecutive_repo_not_found: int

    @property
    def is_permanent(self) -> bool:
        return self.kind is FetchFailureKind.PERMANENT


class IssueFetchUnavailable(RuntimeError):
    """Base class for a classified issue-fetch failure.

    Carries the :class:`FetchFailureVerdict` so call sites and top-level
    handlers can react (skip vs. fail fast) and log the actionable message
    without re-deriving the classification.
    """

    def __init__(self, verdict: FetchFailureVerdict) -> None:
        super().__init__(verdict.summary)
        self.verdict = verdict
        self.summary = verdict.summary
        self.suggested_fix = verdict.suggested_fix


class TransientIssueFetchError(IssueFetchUnavailable):
    """A recoverable issue-fetch failure — skip this fetch and stay up."""


class PermanentIssueFetchError(IssueFetchUnavailable):
    """An unrecoverable issue-fetch failure — fail fast with a clear message."""


class IssueFetchResilience:
    """Single owner of the issue-list fetch resilience policy.

    Classifies repository-host failures and tracks consecutive repo-not-found
    failures so a *persistent* outage is promoted from transient to permanent.
    One instance is shared across startup and the steady-state tick.
    """

    def __init__(
        self,
        repo: str | None,
        *,
        repo_not_found_tolerance: int = DEFAULT_REPO_NOT_FOUND_TOLERANCE,
    ) -> None:
        self._repo = repo or "<unconfigured repo>"
        self._tolerance = max(1, repo_not_found_tolerance)
        self._consecutive_repo_not_found = 0

    def note_success(self) -> None:
        """Record a successful fetch, clearing the repo-not-found streak."""
        self._consecutive_repo_not_found = 0

    def record_failure(self, error: RepositoryHostError) -> FetchFailureVerdict:
        """Classify a repository-host fetch failure and update internal state.

        Increments the consecutive repo-not-found counter for 404s so a
        persistent repo-not-found is promoted to permanent. Returns the verdict
        for the caller to react to (degrade vs. fail fast).
        """
        status = _status_code(error)

        if self._is_auth_failure(status, error):
            return FetchFailureVerdict(
                kind=FetchFailureKind.PERMANENT,
                summary=(
                    f"GitHub authentication/authorization failed for repository "
                    f"'{self._repo}' (HTTP {status})"
                ),
                suggested_fix=(
                    "Check the GitHub token is valid and has read access to this "
                    "repository (an expired token or revoked repo scope looks like "
                    "this)."
                ),
                consecutive_repo_not_found=self._consecutive_repo_not_found,
            )

        if status == 404:
            self._consecutive_repo_not_found += 1
            if self._consecutive_repo_not_found >= self._tolerance:
                return FetchFailureVerdict(
                    kind=FetchFailureKind.PERMANENT,
                    summary=(
                        f"GitHub repository '{self._repo}' not found after "
                        f"{self._consecutive_repo_not_found} consecutive attempts"
                    ),
                    suggested_fix=(
                        "Check `repo.name` in your config is correct and the token "
                        "can access it (a deleted/renamed repo or revoked access "
                        "looks like this)."
                    ),
                    consecutive_repo_not_found=self._consecutive_repo_not_found,
                )
            return FetchFailureVerdict(
                kind=FetchFailureKind.TRANSIENT,
                summary=(
                    f"GitHub returned 404 for repository '{self._repo}' "
                    f"(attempt {self._consecutive_repo_not_found}/{self._tolerance})"
                ),
                suggested_fix=(
                    "Likely a transient blip or eventual-consistency right after a "
                    "rename; keeping the cached queue and retrying."
                ),
                consecutive_repo_not_found=self._consecutive_repo_not_found,
            )

        # Everything else — 5xx, 429/secondary rate limit, transport/network
        # errors, or anything unclassified — is treated as transient. A daemon
        # designed to recover from labels should ride these out rather than die.
        #
        # A non-404 failure also breaks the *consecutive* repo-not-found streak:
        # the promotion to permanent is meant to fire only when 404s persist
        # back-to-back. Without this reset a pattern like ``404, 503, 404`` would
        # falsely promote at tolerance 2, converting an intermittent GitHub
        # outage into a permanent repo-not-found shutdown.
        self._consecutive_repo_not_found = 0
        return FetchFailureVerdict(
            kind=FetchFailureKind.TRANSIENT,
            summary=(
                f"GitHub issue fetch failed transiently for repository "
                f"'{self._repo}': {error}"
            ),
            suggested_fix=(
                "Transient GitHub/network error; keeping the cached queue and "
                "retrying on the next cycle."
            ),
            consecutive_repo_not_found=self._consecutive_repo_not_found,
        )

    def guard(self, fetch: Callable[[], T]) -> T:
        """Run ``fetch`` under the resilience policy.

        On success, clears the repo-not-found streak and returns the result.
        On a repository-host failure, classifies it and raises
        :class:`TransientIssueFetchError` or :class:`PermanentIssueFetchError`.
        Non-repository exceptions (real bugs) are never swallowed.
        """
        try:
            result = fetch()
        except RepositoryHostError as error:
            verdict = self.record_failure(error)
            if verdict.is_permanent:
                raise PermanentIssueFetchError(verdict) from error
            raise TransientIssueFetchError(verdict) from error
        self.note_success()
        return result

    def _is_auth_failure(self, status: int | None, error: RepositoryHostError) -> bool:
        """Return True for a genuine auth/authorization failure.

        401 is always auth. 403 is auth *unless* it is GitHub's rate-limit
        flavor of 403 (which is transient), detected via the response body.
        """
        if status == 401:
            return True
        if status == 403:
            return not _looks_like_rate_limit(error)
        return False


def _status_code(error: RepositoryHostError) -> int | None:
    value = getattr(error, "status_code", None)
    return value if isinstance(value, int) else None


def _looks_like_rate_limit(error: RepositoryHostError) -> bool:
    text = getattr(error, "response_text", None) or str(error)
    return "rate limit" in text.lower()
