"""Helpers for binding PRs to the active branch for an issue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from ..ports.pull_request_tracker import PRInfo


@dataclass(frozen=True)
class AttemptScopedPRs:
    """PRs partitioned by whether they match the active issue branch."""

    expected_branch: str | None
    matching: tuple[PRInfo, ...]
    ignored: tuple[PRInfo, ...]

    @property
    def first_matching(self) -> PRInfo | None:
        return self.matching[0] if self.matching else None


def scope_prs_to_active_issue_branch(
    issue_number: int,
    prs: Iterable[PRInfo],
    *,
    issue_branches: Mapping[int, str] | None = None,
    expected_branch: str | None = None,
) -> AttemptScopedPRs:
    """Partition PRs based on the active branch for an issue.

    If no active branch is known, all PRs are treated as eligible. Once the
    orchestrator knows the active branch, PRs on other branches are assumed to
    belong to prior attempts and are ignored by the caller.
    """

    active_branch = expected_branch
    if active_branch is None and issue_branches is not None:
        active_branch = issue_branches.get(issue_number)

    ordered_prs = tuple(prs)
    if not active_branch:
        return AttemptScopedPRs(
            expected_branch=None,
            matching=ordered_prs,
            ignored=(),
        )

    matching: list[PRInfo] = []
    ignored: list[PRInfo] = []
    for pr in ordered_prs:
        if pr.branch == active_branch:
            matching.append(pr)
        else:
            ignored.append(pr)

    return AttemptScopedPRs(
        expected_branch=active_branch,
        matching=tuple(matching),
        ignored=tuple(ignored),
    )
