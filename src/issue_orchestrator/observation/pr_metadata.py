"""Read labels and milestones from pull requests and their referenced issues."""

import re
from typing import Any
from ..ports.repository_host import RepositoryHost


def pr_labels(pr: Any) -> list[str]:
    labels = getattr(pr, "labels", None)
    if labels is None and isinstance(pr, dict):
        labels = pr.get("labels", [])
    return labels or []


def collect_pr_metadata(repository: RepositoryHost, prs: list[Any]) -> tuple[set[str], list[tuple[int, str]]]:
    """Collect labels and milestones from PRs and their linked issues."""
    all_labels: set[str] = set()
    source_milestones: list[tuple[int, str]] = []

    for pr in prs:
        all_labels.update(pr_labels(pr))
        _collect_linked_issue_metadata(repository, pr, all_labels, source_milestones)

    return all_labels, source_milestones

def _collect_linked_issue_metadata(
    repository: RepositoryHost,
    pr: Any,
    all_labels: set[str],
    source_milestones: list[tuple[int, str]],
) -> None:
    """Collect metadata from issues linked to a PR."""
    matches = re.findall(r'#(\d+)', (getattr(pr, 'body', '') or "") + " " + pr.title)
    for match in matches:
        issue_num = int(match)
        issue = repository.get_issue(issue_num)
        if not issue:
            continue
        all_labels.update(issue.labels)
        if issue.milestone and issue.milestone_number:
            milestone_tuple = (issue.milestone_number, issue.milestone)
            if milestone_tuple not in source_milestones:
                source_milestones.append(milestone_tuple)

