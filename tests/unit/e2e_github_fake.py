"""An in-memory e2e test repository for the e2e cleanup owner's tests (#9523).

It answers the reads ``tests/e2e/fixtures/artifact_cleanup.CleanupGitHub``
makes and records every write, so a test asserts on what would have been
closed or deleted on GitHub.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from issue_orchestrator.ports.pull_request_tracker import PRInfo


def pull_request(
    number: int,
    branch: str,
    labels: Iterable[str],
    *,
    body: str = "",
    state: str = "open",
    base: str = "main",
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
    )


class IncompleteReadError(RuntimeError):
    """A complete read that could not be completed (a capped or failed scan)."""


class FakeE2EGitHub:
    """PRs and branches of one repository; ``closed`` and ``deleted_branches``
    are every write made to it.

    ``cached_for_branch`` is what a cached per-branch read would answer: it is
    there to prove the owner never decides from it. ``incomplete_reads`` makes
    the complete reads raise, as a capped scan does.
    """

    def __init__(self, prs: Iterable[PRInfo], *, branches: Iterable[str] = ()) -> None:
        self._prs = {pr.number: pr for pr in prs}
        self.branches = {pr.branch for pr in self._prs.values() if pr.branch} | set(branches)
        self.closed: list[int] = []
        self.deleted_branches: list[str] = []
        self.cached_for_branch: dict[str, list[PRInfo]] = {}
        self.incomplete_reads = False

    @property
    def writes(self) -> tuple[list[int], list[str]]:
        return self.closed, self.deleted_branches

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

    def delete_branch(self, branch: str) -> None:
        self.deleted_branches.append(branch)
        self.branches.discard(branch)

    def branch_exists(self, branch: str) -> bool:
        return branch in self.branches
