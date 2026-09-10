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
    """Resolve only the supplied manifest or problem issues, never the whole repo.

    Missing head/link facts give no grant; they cannot become guessed authority.
    A transport failure propagates instead of silently shortening the grant.
    """
    numbers = set(pr_numbers)
    for issue_number in issue_numbers:
        numbers.update(
            pr.number for pr in repository.get_prs_for_issue(issue_number, state="all")
        )
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
