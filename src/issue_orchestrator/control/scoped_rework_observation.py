"""Capture the exact PR facts a tech-lead launch grants for scoped rework."""

from __future__ import annotations

import logging
from collections.abc import Sequence, Mapping
from ..ports.pull_request_tracker import PRInfo

from ..domain.scoped_rework import ReworkTarget
from ..ports import RepositoryHost
from .review_scope import extract_issue_number_from_pr, pr_fields_reference_issue

logger = logging.getLogger(__name__)


def observe_rework_targets(
    repository: RepositoryHost,
    *,
    pr_numbers: Sequence[int],
    issue_numbers: Sequence[int],
    expected_heads: Mapping[int, str] | None = None,
) -> tuple[ReworkTarget, ...]:
    """Resolve only the supplied manifest or problem issues' PRs.

    Problem issues resolve without the search API: one complete listing of open
    PRs, matched to their issue the same way the grant below links them, plus
    the merged PRs that reference them (rework on a merged PR files a forward
    fix). The references come from each issue's timeline, so they include a
    merged partial PR ("Refs #N", #7288) as well as a closing one; the link
    check below still decides which of them belong to the issue. It used to be
    one ``/search/issues`` call per issue: a health review over ~50 blocked
    issues blew GitHub's 30-per-minute search budget and could never launch.
    Closed-unmerged PRs take no rework. A merged PR whose body never names the
    issue (linked only by its branch name) is not found.

    Missing head/link facts give no grant; they cannot become guessed authority.
    A transport failure, or a listing that cannot be proven complete,
    propagates instead of silently shortening the grant.
    """
    numbers = set(pr_numbers)
    wanted = set(issue_numbers)
    if wanted:
        numbers.update(
            pr.number
            for pr in repository.list_open_prs_complete()
            if extract_issue_number_from_pr(pr) in wanted
        )
        numbers.update(repository.merged_prs_referencing_issues(sorted(wanted)))
    targets: list[ReworkTarget] = []
    for number in sorted(numbers):
        pr = repository.get_pr(number)
        if not isinstance(pr, PRInfo) or not pr.head_sha:
            logger.warning(
                "No scoped rework grant for PR #%s: missing PR/head fact", number
            )
            continue
        if expected_heads is not None and expected_heads.get(number) != pr.head_sha:
            logger.warning(
                "No scoped rework grant for PR #%s: head changed during evidence download",
                number,
            )
            continue
        linked = extract_issue_number_from_pr(pr)
        if not pr_fields_reference_issue(
            branch=pr.branch, title="", body=pr.body, issue_numbers=[linked]
        ):
            continue
        if number not in pr_numbers and linked not in issue_numbers:
            continue
        issue = repository.get_issue(linked)
        if issue is None:
            continue
        targets.append(
            ReworkTarget(
                repository=issue.key.scope(),
                pr_number=number,
                issue_number=linked,
                head_sha=pr.head_sha,
                branch=pr.branch,
                pr_labels=tuple(pr.labels),
                issue_labels=tuple(issue.labels),
            )
        )
    return tuple(targets)
