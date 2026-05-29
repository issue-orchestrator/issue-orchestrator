"""PR collision and branch-remediation helpers for completion processing."""

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..domain.pr_attempt_scope import scope_prs_to_active_issue_branch
from ..ports.pull_request_tracker import PRInfo
from ..ports.working_copy import PushResult

logger = logging.getLogger(__name__)


# These protocols intentionally narrow the broader PR/working-copy ports to the
# methods this policy needs. Keeping the seam small avoids coupling completion
# collision handling to unrelated adapter capabilities.
class CompletionPrAdapter(Protocol):
    def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        draft: bool | None = None,
    ) -> PRInfo: ...

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]: ...
    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]: ...


class CompletionGitAdapter(Protocol):
    def push(
        self,
        worktree: Path,
        remote: str = "origin",
        set_upstream: bool = True,
        skip_hooks: bool = False,
    ) -> PushResult: ...

    def create_branch_from_current(self, worktree: Path, branch: str) -> None: ...
    def list_branch_names(self, worktree: Path) -> list[str]: ...


class NoCommitsBetweenError(RuntimeError):
    """Raised when GitHub rejects PR creation because base and head match."""

    def __init__(self, *, base: str, head: str) -> None:
        self.base = base
        self.head = head
        super().__init__(
            f"Cannot create PR: no commits between {base} and {head}. "
            "Possible causes: (1) agent didn't make any changes, "
            "(2) work already merged via another PR, "
            "(3) commits lost during rebase. "
            "Human review required."
        )


def create_pr_with_collision_handling(
    *,
    pr_adapter: CompletionPrAdapter,
    git_adapter: CompletionGitAdapter,
    base_branch: Callable[[], str],
    pr_collision_strategy: str,
    worktree: Path,
    pr_title: str,
    pr_body: str,
    branch: str,
    issue_number: int,
    actions_taken: list[str],
    skip_hooks: bool,
    draft: bool,
) -> PRInfo | None:
    """Create a PR, switching to a suffixed branch when a prior PR owns the branch."""
    base = base_branch()
    try:
        return pr_adapter.create_pr(
            title=pr_title,
            body=pr_body,
            head=branch,
            base=base,
            draft=draft,
        )
    except Exception as exc:
        if pr_collision_strategy == "new_branch" and is_pr_collision_error(exc):
            new_branch = switch_to_suffixed_branch(
                git_adapter=git_adapter,
                worktree=worktree,
                branch=branch,
                issue_number=issue_number,
                actions_taken=actions_taken,
                skip_hooks=skip_hooks,
            )
            try:
                return pr_adapter.create_pr(
                    title=pr_title,
                    body=pr_body,
                    head=new_branch,
                    base=base,
                    draft=draft,
                )
            except Exception as retry_exc:
                if _is_raw_no_commits_error(retry_exc):
                    raise NoCommitsBetweenError(base=base, head=new_branch) from retry_exc
                raise
        if _is_raw_no_commits_error(exc):
            raise NoCommitsBetweenError(base=base, head=branch) from exc
        raise


def get_open_pr_for_issue(
    pr_adapter: CompletionPrAdapter,
    issue_number: int,
    *,
    expected_branch: str | None = None,
) -> PRInfo | None:
    try:
        prs = pr_adapter.get_prs_for_issue(issue_number, state="open")
    except Exception as exc:
        logger.warning("Failed to query open PRs for issue %s: %s", issue_number, exc)
        return None
    matching_prs = [pr for pr in prs if pr_matches_issue(pr, issue_number)]
    scoped = scope_prs_to_active_issue_branch(
        issue_number,
        matching_prs,
        expected_branch=expected_branch,
    )
    for pr in scoped.ignored:
        logger.info(
            "Ignoring open PR from prior attempt for issue #%d: pr=%d branch=%s expected_branch=%s",
            issue_number,
            pr.number,
            pr.branch,
            scoped.expected_branch,
        )
    return scoped.first_matching


def maybe_switch_branch_for_pr_collision(
    *,
    pr_adapter: CompletionPrAdapter,
    git_adapter: CompletionGitAdapter,
    worktree: Path,
    branch: str,
    issue_number: int,
    actions_taken: list[str],
    skip_hooks: bool,
) -> str:
    try:
        prs = pr_adapter.get_prs_for_branch(branch, state="all")
    except Exception as exc:
        logger.warning("Failed to query PRs for branch %s: %s", branch, exc)
        return branch
    if not prs:
        return branch
    for pr in prs:
        if pr.state.lower() == "open" and pr_matches_issue(pr, issue_number):
            return branch
    return switch_to_suffixed_branch(
        git_adapter=git_adapter,
        worktree=worktree,
        branch=branch,
        issue_number=issue_number,
        actions_taken=actions_taken,
        skip_hooks=skip_hooks,
    )


def switch_to_suffixed_branch(
    *,
    git_adapter: CompletionGitAdapter,
    worktree: Path,
    branch: str,
    issue_number: int,
    actions_taken: list[str],
    skip_hooks: bool,
) -> str:
    new_branch = next_branch_name(git_adapter, worktree, branch)
    git_adapter.create_branch_from_current(worktree, new_branch)
    push_result = git_adapter.push(worktree, skip_hooks=skip_hooks)
    if not push_result.success:
        raise RuntimeError(f"Failed to push new branch {new_branch}: {push_result.message}")
    actions_taken.append(f"Switched to new branch {new_branch}")
    logger.info(
        "PR collision remediation for issue #%d: branch=%s -> %s",
        issue_number,
        branch,
        new_branch,
    )
    return new_branch


def next_branch_name(git_adapter: CompletionGitAdapter, worktree: Path, branch: str) -> str:
    base = re.sub(r"-r\d+$", "", branch)
    existing = git_adapter.list_branch_names(worktree)
    pattern = re.compile(rf"^{re.escape(base)}-r(\d+)$")
    max_suffix = 0
    for name in existing:
        match = pattern.match(name)
        if match:
            max_suffix = max(max_suffix, int(match.group(1)))
    return f"{base}-r{max_suffix + 1}"


def is_pr_collision_error(error: Exception) -> bool:
    message = str(error).lower()
    return "pull request" in message and "already exists" in message


def is_no_commits_error(error: Exception) -> bool:
    """Return whether a PR creation failure was normalized as no commits."""
    return isinstance(error, NoCommitsBetweenError)


def _is_raw_no_commits_error(error: Exception) -> bool:
    """Detect the raw GitHub 422 message at the adapter boundary."""
    message = str(error).lower()
    return "no commits between" in message


def pr_matches_issue(pr: PRInfo, issue_number: int) -> bool:
    if pr.branch and pr.branch.startswith(f"{issue_number}-"):
        return True
    if pr.title and f"#{issue_number}" in pr.title:
        return True
    return False
