"""Authoritative remote observations used by exact publication."""

from dataclasses import dataclass
from enum import StrEnum

from .validated_work import require_positive, require_sha, require_text


class PublicationRemoteError(RuntimeError):
    """A failed or incomplete read/write; never evidence of absence."""


class PrCreateRejection(StrEnum):
    """The host's definite refusals of a PR create (GitHub: HTTP 422).

    A refusal is an answer, not a lost response: the host processed the request
    and created nothing. Each kind names what the publication owner may do next.
    """

    # The head has nothing the base lacks. Retrying the same exact head can
    # never succeed; a human decides what the retained work is (#7346).
    NO_COMMITS = "no_commits"
    # A PR for this head already exists. Creation must not be retried; the
    # existing PR is adopted only through this operation's exact marker.
    ALREADY_EXISTS = "already_exists"
    # Any other validation refusal (base/head invalid, ...): permanent for this
    # exact request.
    INVALID = "invalid"


class PublicationPrCreateRejected(PublicationRemoteError):
    """The host definitely refused PR creation; no PR was created by this call."""

    def __init__(self, rejection: PrCreateRejection, detail: str) -> None:
        if type(rejection) is not PrCreateRejection:
            raise ValueError("PR create rejection must be typed")
        super().__init__(detail)
        self.rejection = rejection


class PublicationPrState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


@dataclass(frozen=True, slots=True)
class PublicationPullRequest:
    number: int
    url: str
    head_repo: str
    base_repo: str
    branch: str
    base_branch: str
    head_sha: str
    state: PublicationPrState
    body: str

    def __post_init__(self) -> None:
        require_sha(self.head_sha)
        require_positive(self.number, "pr_number")
        for name in ("url", "head_repo", "base_repo", "branch", "base_branch"):
            require_text(getattr(self, name), name)
        if type(self.body) is not str or type(self.state) is not PublicationPrState:
            raise ValueError("invalid authoritative pull request")


def publication_marker(issue_number: int, branch: str) -> str:
    """Stable across attempts and heads, scoped to one issue branch."""
    return (
        f"<!-- issue-orchestrator:publication issue={issue_number} branch={branch} -->"
    )


def attributed_publication_body(body: str, issue_number: int, branch_name: str) -> str:
    """Use one exact attribution line for live and retried PR creation."""
    marker = publication_marker(issue_number, branch_name)
    return body if marker in body.splitlines() else f"{body}\n\n{marker}"
