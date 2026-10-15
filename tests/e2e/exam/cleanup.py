"""The exam's strict teardown of what a run left on GitHub.

Every close and delete goes through the e2e suite's one cleanup owner
(``tests/e2e/fixtures/artifact_cleanup.py``) in the run's ``ExamRunScope``:
the issues the run created or labelled with its own unique run label, and the
branches it recorded creating (``ExamRun.branches``). Every removal is strict:
a PR is closed AND its branch deleted, and both are verified gone, or the step
raises. ``cleanup_steps.run_all_steps`` keeps the other steps running and
raises every failure together.
"""

from __future__ import annotations

import logging
from typing import Iterable

from tests.e2e.exam.observe import linked_pull_requests
from tests.e2e.fixtures import _github_adapter
from tests.e2e.fixtures.artifact_cleanup import (
    CleanupRefusedError,
    E2EArtifactCleanup,
    ExamRunScope,
)

logger = logging.getLogger(__name__)


class ExamCleanupError(RuntimeError):
    """Something the run created is still on GitHub."""


def _owner(repo: str) -> E2EArtifactCleanup:
    return E2EArtifactCleanup.for_test_repo(repo, github=_github_adapter(repo))


def remove_branch(repo: str, scope: ExamRunScope, branch: str) -> None:
    """Close any open PR of ``branch``, delete the branch, verify it is gone."""
    _owner(repo).delete_branch(scope, branch)
    if _github_adapter(repo).branch_exists(branch):
        raise ExamCleanupError(f"branch {branch} still exists after deletion")


def remove_pr(repo: str, scope: ExamRunScope, pr_number: int) -> None:
    """Close PR ``pr_number`` and delete its branch, verifying both."""
    adapter = _github_adapter(repo)
    pr = adapter.get_pr(pr_number)
    if pr is None:
        raise ExamCleanupError(f"PR #{pr_number} could not be read for cleanup")
    _owner(repo).close_pr(scope, pr_number)
    closed = adapter.get_pr(pr_number)
    if closed is None or closed.state == "open":
        raise ExamCleanupError(f"PR #{pr_number} is still open after closing it")
    if pr.branch and adapter.branch_exists(pr.branch):
        raise ExamCleanupError(f"branch {pr.branch} still exists after deletion")
    logger.info("[EXAM] removed PR #%d and branch %s", pr_number, pr.branch)


def teardown_run(
    repo: str, run_label: str, created: Iterable[int], branches: Iterable[str]
) -> None:
    """Remove every PR — open or closed — of every issue the run touched, with
    its branch (a closed PR can leave its branch behind, e.g. after a partial
    reset).

    "Touched" is the issues the harness created plus every issue carrying the
    run label — the engine files some itself (tech-lead anchors, follow-ups,
    case files) and can publish PRs for them. Recovery-published PRs need not
    carry the e2e cleanup labels, so label-based reconciliation cannot be
    relied on to find them. A candidate the run's evidence does not admit (its
    head is not a branch the run recorded and its body links none of the
    run's issues) is retained and reported, never removed. Branches with no
    PR are ``delete_registered_branches``'s; the issues are the caller's.
    """
    labelled = [issue.number for issue in _github_adapter(repo).list_issues(labels=[run_label], state="all")]
    owned = sorted(set(created) | set(labelled))
    scope = ExamRunScope(issue_numbers=frozenset(owned), run_branches=frozenset(branches))
    for number in owned:
        for pr in linked_pull_requests(repo, number, state="all"):
            try:
                remove_pr(repo, scope, pr.number)
            except CleanupRefusedError as exc:
                logger.warning("[EXAM] retained PR #%d, not provably this run's: %s", pr.number, exc)


def delete_registered_branches(repo: str, branches: Iterable[str]) -> None:
    """Remove every branch the run recorded creating, whatever state it reached
    (a branch whose PR was never created is removed too)."""
    recorded = tuple(dict.fromkeys(branches))
    scope = ExamRunScope(issue_numbers=frozenset(), run_branches=frozenset(recorded))
    for branch in recorded:
        remove_branch(repo, scope, branch)
