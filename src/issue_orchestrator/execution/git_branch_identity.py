"""Branch naming compatibility at the working-copy boundary."""

import re


def issue_number_from_branch(branch: str | None) -> int | None:
    from ..adapters.worktree._worktree import extract_issue_number_from_branch

    if not branch:
        return None
    issue_number = extract_issue_number_from_branch(branch)
    if issue_number is not None:
        return issue_number
    for pattern in (r"issue-(\d+)", r"/(\d+)-"):
        match = re.search(pattern, branch)
        if match:
            return int(match.group(1))
    return None
