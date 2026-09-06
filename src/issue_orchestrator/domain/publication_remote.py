"""Authoritative remote observations used by exact publication."""

from dataclasses import dataclass
from enum import StrEnum

from .validated_work import require_positive, require_sha, require_text


class PublicationRemoteError(RuntimeError):
    """A failed or incomplete read/write; never evidence of absence."""


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
