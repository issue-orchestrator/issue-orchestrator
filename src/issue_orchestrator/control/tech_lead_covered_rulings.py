"""The rulings of the work a tech-lead run covers (#8347).

A batch review approved PRs against rulings it was never told (porchpin#379's
failure class): the launch prompt carried only the anchor issue's rulings. A
tech-lead run is bound by the rulings of every other issue whose work it
covers - a batch review's PRs' issues and a health review's problem cohort -
read fresh, through the standing-rulings owner, at launch and again at every
validation retry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from .review_scope import issues_of_pr

if TYPE_CHECKING:
    from ..ports import RepositoryHost
    from ..ports.launch_prompt import LaunchPromptProvider
    from .tech_lead_run_inputs import LaunchAuthorityTransfer


def covered_issues(
    pr_issues: Mapping[int, tuple[int, ...]],
    problem_issue_numbers: tuple[int, ...],
    triaged: frozenset[int],
    *,
    anchor: int,
) -> dict[int, tuple[int, ...]]:
    """The other issues whose work a tech-lead run covers, with their PRs in it:
    a batch's PRs' issues (*pr_issues*), a health review's problem cohort and
    the blocked items it triages. The anchor's own rulings come with its launch
    prompt, so they are not repeated."""
    covered = dict(pr_issues)
    for number in (*problem_issue_numbers, *sorted(triaged)):
        covered.setdefault(number, ())
    covered.pop(anchor, None)
    return covered


def retried_covered_rulings(
    launch_prompt: "LaunchPromptProvider",
    repository_host: "RepositoryHost",
    carried: "LaunchAuthorityTransfer | None",
    *,
    repo_slug: str,
) -> str | None:
    """The rulings binding a RETRIED tech-lead run, read fresh now: a ruling
    recorded or retired since the original launch binds the retry. Its scope is
    the carried create-once authority (never re-sampled); each manifest PR's
    issues are read from GitHub, not from the agent-writable manifest copy. A PR
    GitHub no longer has binds nothing. Raises as
    :meth:`LaunchPromptProvider.covered_rulings` does."""
    if carried is None:
        return None
    authority = carried.authority
    pr_issues: dict[int, list[int]] = {}
    for number in authority.manifest_pr_numbers:
        pr = repository_host.get_pr(number)
        for issue_number in issues_of_pr(pr, repo_slug=repo_slug) if pr is not None else ():
            pr_issues.setdefault(issue_number, []).append(number)
    return launch_prompt.covered_rulings(covered_issues(
        {issue_number: tuple(sorted(prs)) for issue_number, prs in pr_issues.items()},
        authority.problem_issue_numbers,
        authority.triage_issue_numbers(),
        anchor=authority.anchor_issue_number,
    ))
