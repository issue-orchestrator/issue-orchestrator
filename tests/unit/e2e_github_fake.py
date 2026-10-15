"""An in-memory e2e test repository for the e2e cleanup owner's tests (#9523).

It answers the reads ``tests/e2e/fixtures/artifact_cleanup.CleanupGitHub``
makes and records every write, so a test asserts on what would have been
closed or deleted on GitHub.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from issue_orchestrator.ports.pull_request_tracker import PRInfo


def pull_request(
    number: int,
    branch: str,
    labels: Iterable[str],
    *,
    body: str = "",
    state: str = "open",
    base: str = "main",
    head_sha: str | None = None,
) -> PRInfo:
    return PRInfo(
        number=number,
        title=f"PR {number}",
        url=f"https://example.test/pull/{number}",
        branch=branch,
        body=body,
        state=state,
        labels=list(labels),
        base_branch=base,
        head_sha=head_sha or sha_of(branch),
    )


def sha_of(name: str) -> str:
    """A stable 40-hex commit SHA standing for ``name``'s head."""
    return hashlib.sha1(name.encode()).hexdigest()


@dataclass(frozen=True)
class FakeIssue:
    number: int
    labels: tuple[str, ...]
    state: str = "open"


def issue(number: int, labels: Iterable[str], *, state: str = "open") -> FakeIssue:
    return FakeIssue(number=number, labels=tuple(labels), state=state)


class IncompleteReadError(RuntimeError):
    """A complete read that could not be completed (a capped or failed scan)."""


class FakeE2EGitHub:
    """PRs, branches, issues and labels of one repository; ``closed``,
    ``deleted_branches``, ``closed_issues``, ``comments`` and
    ``deleted_labels`` are every write made to it.

    Branch heads default to each PR's ``head_sha``; ``heads`` overrides or
    adds them. ``cached_for_branch`` is what a cached per-branch read would
    answer: it is there to prove the owner never decides from it.
    ``incomplete_reads`` makes the complete reads raise, as a capped scan does.
    """

    def __init__(
        self,
        prs: Iterable[PRInfo] = (),
        *,
        branches: Iterable[str] = (),
        heads: Mapping[str, str] | None = None,
        issues: Iterable[FakeIssue] = (),
        labels: Iterable[str] = (),
    ) -> None:
        self._prs = {pr.number: pr for pr in prs}
        self.heads: dict[str, str] = {
            pr.branch: pr.head_sha or sha_of(pr.branch) for pr in self._prs.values() if pr.branch
        }
        self.heads.update({branch: sha_of(branch) for branch in branches})
        self.heads.update(heads or {})
        self._issues = {item.number: item for item in issues}
        self.labels = set(labels)
        self.closed: list[int] = []
        self.deleted_branches: list[str] = []
        self.closed_issues: list[int] = []
        self.comments: list[tuple[int, str]] = []
        self.deleted_labels: list[str] = []
        self.cached_for_branch: dict[str, list[PRInfo]] = {}
        self.incomplete_reads = False
        #: False: a branch deletion GitHub accepts but that leaves the branch.
        self.delete_works = True

    @property
    def writes(self) -> tuple[list[int], list[str]]:
        return self.closed, self.deleted_branches

    @property
    def all_writes(self) -> dict[str, list[Any]]:
        return {
            "closed_prs": self.closed,
            "deleted_branches": self.deleted_branches,
            "closed_issues": self.closed_issues,
            "comments": self.comments,
            "deleted_labels": self.deleted_labels,
        }

    # -- PRs and branches

    def get_pr(self, pr_number: int) -> PRInfo | None:
        pr = self._prs.get(pr_number)
        if pr is None or pr_number not in self.closed:
            return pr
        return replace(pr, state="closed")

    def _current(self) -> list[PRInfo]:
        return [pr for number in self._prs if (pr := self.get_pr(number)) is not None]

    def get_prs_with_label(self, label: str, state: str = "open") -> list[PRInfo]:
        return [
            pr for pr in self._current()
            if label in pr.labels and state in ("all", pr.state)
        ]

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]:
        return self.cached_for_branch.get(branch, [])

    def get_open_prs_for_branch_complete(self, branch: str) -> list[PRInfo]:
        if self.incomplete_reads:
            raise IncompleteReadError(f"open PR scan for {branch} incomplete")
        return [pr for pr in self._current() if pr.branch == branch and pr.state == "open"]

    def list_open_prs_complete(self) -> list[PRInfo]:
        """Like the GraphQL walk: complete, but without labels."""
        if self.incomplete_reads:
            raise IncompleteReadError("open PR listing incomplete")
        return [replace(pr, labels=[]) for pr in self._current() if pr.state == "open"]

    def close_pr(self, pr_number: int) -> None:
        self.closed.append(pr_number)

    def branch_head(self, branch: str) -> str | None:
        return self.heads.get(branch)

    def branch_exists(self, branch: str) -> bool:
        return branch in self.heads

    def delete_branch(self, branch: str) -> None:
        self.deleted_branches.append(branch)
        if self.delete_works:
            self.heads.pop(branch, None)

    # -- Issues

    def get_issue(self, issue_number: int) -> FakeIssue | None:
        found = self._issues.get(issue_number)
        if found is None or issue_number not in self.closed_issues:
            return found
        return replace(found, state="closed")

    def list_issues(
        self, labels: list[str] | None = None, milestone: str | None = None,
        state: str = "open", limit: int = 100,
    ) -> list[FakeIssue]:
        current = [found for number in self._issues if (found := self.get_issue(number))]
        return [
            found for found in current
            if all(label in found.labels for label in labels or [])
            and state in ("all", found.state)
        ][:limit]

    def add_comment(self, issue_or_pr_number: int, body: str) -> str:
        self.comments.append((issue_or_pr_number, body))
        return "comment"

    def update_issue_state(self, issue_number: int, state: str) -> None:
        assert state == "closed"
        self.closed_issues.append(issue_number)

    # -- Labels

    def list_all_labels(self) -> list[dict[str, Any]]:
        return [{"name": name} for name in sorted(self.labels)]

    def delete_label(self, name: str) -> None:
        self.deleted_labels.append(name)
        self.labels.discard(name)
