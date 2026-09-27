"""The exam's one owner of removing what a run left on GitHub.

Every removal is strict: a PR is closed AND its branch deleted, and both are
verified gone, or the step raises. The shared e2e helper ``flows.close_pr``
swallows branch-deletion failures, which would let an engine-pushed branch
survive a run that reported clean. ``cleanup_steps.run_all_steps`` keeps
the other steps running and raises every failure together.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Iterable

from tests.e2e.exam.observe import linked_pull_requests
from tests.e2e.exam.run_identity import github_remote
from tests.e2e.fixtures import _github_adapter

logger = logging.getLogger(__name__)


class ExamCleanupError(RuntimeError):
    """Something the run created is still on GitHub."""


def remove_branch(repo: str, branch: str) -> None:
    """Close any open PR of ``branch``, delete the branch, verify it is gone."""
    adapter = _github_adapter(repo)
    for pr in adapter.get_prs_for_branch(branch):
        if pr.state == "open":
            adapter.close_pr(pr.number)
    if adapter.branch_exists(branch):
        adapter.delete_branch(branch)
    if adapter.branch_exists(branch):
        raise ExamCleanupError(f"branch {branch} still exists after deletion")


def remove_pr(repo: str, pr_number: int) -> None:
    """Close PR ``pr_number`` and delete its branch, verifying both."""
    adapter = _github_adapter(repo)
    pr = adapter.get_pr(pr_number)
    if pr is None:
        raise ExamCleanupError(f"PR #{pr_number} could not be read for cleanup")
    if pr.state == "open":
        adapter.close_pr(pr_number)
    closed = adapter.get_pr(pr_number)
    if closed is None or closed.state == "open":
        raise ExamCleanupError(f"PR #{pr_number} is still open after closing it")
    remove_branch(repo, pr.branch)
    logger.info("[EXAM] removed PR #%d and branch %s", pr_number, pr.branch)


def teardown_run(repo: str, run_label: str, created: Iterable[int]) -> None:
    """Remove every PR — open or closed — of every issue the run touched, with
    its branch (a closed PR can leave its branch behind, e.g. after a partial
    reset).

    "Touched" is the issues the harness created plus every issue carrying the
    run label — the engine files some itself (tech-lead anchors, follow-ups,
    case files) and can publish PRs for them. Recovery-published PRs need not
    carry the e2e cleanup labels, so label-based reconciliation cannot be
    relied on to find them. The issues themselves are closed by the caller.
    """
    labelled = [issue.number for issue in _github_adapter(repo).list_issues(labels=[run_label], state="all")]
    owned = sorted(set(created) | set(labelled))
    for number in owned:
        for pr in linked_pull_requests(repo, number, state="all"):
            remove_pr(repo, pr.number)
    # A branch the engine pushed for an owned issue whose PR was never
    # created is found by the engine's branch naming (``<issue>-...``).
    for branch in branches_for_issues(repo, owned):
        remove_branch(repo, branch)


def branches_for_issues(repo: str, issue_numbers: Iterable[int]) -> list[str]:
    """Every remote branch named ``<issue>-...`` for the given issues.

    ``git ls-remote`` returns ALL heads in one complete read and spends no
    GitHub API budget; it addresses the run's repository (github_remote).
    """
    wanted = {str(number) for number in issue_numbers}
    if not wanted:
        return []
    result = subprocess.run(
        ["git", "ls-remote", "--heads", github_remote(repo)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise ExamCleanupError(f"could not list remote branches of {repo}: {result.stderr.strip()}")
    return parse_issue_branches(result.stdout, wanted)


def parse_issue_branches(ls_remote: str, issue_numbers: Iterable[str]) -> list[str]:
    """Branches from ``git ls-remote --heads`` output whose name is exactly
    ``<issue>-...`` for one of ``issue_numbers`` (so #7 never matches 77-x)."""
    wanted = set(issue_numbers)
    branches: list[str] = []
    for line in ls_remote.splitlines():
        ref = line.split("\t", 1)[-1].strip()
        name = ref.removeprefix("refs/heads/")
        prefix, sep, _rest = name.partition("-")
        if sep and prefix in wanted:
            branches.append(name)
    return sorted(branches)


def delete_registered_branches(repo: str, branches: Iterable[str]) -> None:
    """Remove every branch the harness pushed, whatever state seeding reached
    (a branch whose PR was never created is removed too)."""
    for branch in branches:
        remove_branch(repo, branch)
