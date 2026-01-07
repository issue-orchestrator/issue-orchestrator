"""E2E cleanup functions for test artifacts."""

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .github_client import _github_adapter
from .orchestrator_process import _keep_artifacts, _keep_remote_artifacts

logger = logging.getLogger(__name__)

# Default label for e2e test data
DEFAULT_E2E_FILTER_LABEL = "test-data"


def cleanup_local_worktrees(worktree_base: Path | None = None) -> int:
    """Clean up local e2e worktrees.

    Args:
        worktree_base: Base directory for worktrees. Defaults to /tmp/e2e-worktrees.
    """
    if worktree_base is None:
        worktree_base = Path("/tmp/e2e-worktrees")
    if worktree_base.exists():
        count = 0
        for item in worktree_base.iterdir():
            if item.is_dir():
                try:
                    shutil.rmtree(item)
                    count += 1
                except Exception as e:
                    logger.warning("Failed to remove worktree %s: %s", item, e)
        if count > 0:
            logger.info("[E2E CLEANUP] Removed %d local worktrees from %s", count, worktree_base)
    return 0


def cleanup_tmux_sessions(tmux_session: str = "orchestrator") -> None:
    """Clean up tmux sessions from previous e2e runs.

    Args:
        tmux_session: Name of the tmux session to kill. Defaults to "orchestrator".
    """
    result = subprocess.run(
        ["tmux", "kill-session", "-t", tmux_session],
        capture_output=True
    )
    if result.returncode == 0:
        logger.info("[E2E CLEANUP] Killed stale tmux session: %s", tmux_session)


def run_cleanup_step(name: str, fn, timeout_s: int) -> int:
    """Run a cleanup step with a hard wall-clock timeout."""
    start = time.monotonic()
    result: dict[str, int] = {}

    def _runner() -> None:
        try:
            result["value"] = int(fn())
        except Exception:
            result["value"] = 0

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        logger.warning("[E2E CLEANUP] %s timed out after %ds; skipping", name, timeout_s)
        return 0
    elapsed = time.monotonic() - start
    logger.info("[E2E CLEANUP] %s completed in %.1fs", name, elapsed)
    return result.get("value", 0)


def verify_cleanup_items(
    name: str,
    items: list,
    check_fn,
    retries: int = 1,
    retry_delay_s: float = 2.0,
) -> int:
    """Verify cleanup items, retrying once to allow eventual consistency."""
    remaining = list(items)
    for attempt in range(retries + 1):
        if not remaining:
            return 0
        still = []
        for item in remaining:
            if check_fn(item):
                continue
            still.append(item)
        if not still:
            return 0
        remaining = still
        if attempt < retries:
            logger.info(
                "[E2E CLEANUP] %s verify pending=%d; retrying in %.1fs",
                name,
                len(remaining),
                retry_delay_s,
            )
            time.sleep(retry_delay_s)
    logger.warning("[E2E CLEANUP] %s verify incomplete; remaining=%d", name, len(remaining))
    return len(remaining)


def cleanup_remote_branches(repo: str) -> int:
    """Clean up remote branches matching e2e patterns (orphaned from crashed runs)."""
    e2e_patterns = ["e2e-", "-e2e-", "-test-"]
    branches_deleted = 0
    branches_attempted: list[str] = []
    deadline = time.monotonic() + 30

    # 1. Delete branches for ALL e2e PRs (including closed)
    try:
        prs = _github_adapter(repo).list_prs(state="all", limit=100)
        for pr in prs:
            branch = pr.branch or ""
            if any(pattern in branch.lower() for pattern in e2e_patterns):
                if time.monotonic() > deadline:
                    logger.warning("[E2E CLEANUP] Branch cleanup time budget exceeded; stopping early")
                    return branches_deleted
                try:
                    _github_adapter(repo).delete_branch(branch)
                    branches_attempted.append(branch)
                    logger.info("[E2E CLEANUP] Deleted branch for PR #%d: %s", pr.number, branch)
                    branches_deleted += 1
                except Exception:
                    logger.warning("[E2E CLEANUP] Failed deleting branch for PR #%s: %s", pr.number, branch)
    except Exception as exc:
        logger.warning("[E2E CLEANUP] Failed to list PRs for branch cleanup: %s", exc)

    # 2. Also check for orphan branches not associated with any PR
    try:
        for branch in _github_adapter(repo).list_branches():
            if any(pattern in branch.lower() for pattern in e2e_patterns):
                logger.info("[E2E CLEANUP] Deleting orphan branch: %s", branch)
                if time.monotonic() > deadline:
                    logger.warning("[E2E CLEANUP] Branch cleanup time budget exceeded; stopping early")
                    return branches_deleted
                try:
                    _github_adapter(repo).delete_branch(branch)
                    branches_attempted.append(branch)
                    branches_deleted += 1
                except Exception:
                    logger.warning("[E2E CLEANUP] Failed deleting orphan branch: %s", branch)
    except Exception as exc:
        logger.warning("[E2E CLEANUP] Failed to list remote branches: %s", exc)

    def _branch_gone(branch: str) -> bool:
        try:
            return not _github_adapter(repo).branch_exists(branch)
        except Exception:
            return True

    verify_cleanup_items(
        "Branch cleanup",
        branches_attempted,
        _branch_gone,
        retries=1,
        retry_delay_s=3.0,
    )
    return branches_deleted


def cleanup_prs(repo: str) -> int:
    """Clean up PRs with test labels or e2e branch patterns."""
    labels_to_cleanup = [DEFAULT_E2E_FILTER_LABEL, "needs-code-review", "code-reviewed"]
    e2e_branch_patterns = ["e2e-", "-test-", "-concurrent-"]
    closed_pr_nums: set[int] = set()
    branches_attempted: list[str] = []

    adapter = _github_adapter(repo)

    # First, clean up OPEN PRs with specific labels
    for label in labels_to_cleanup:
        try:
            items = adapter.get_prs_with_label(label, state="open")
            for item in items:
                pr_num = item.number
                if not pr_num or pr_num in closed_pr_nums:
                    continue
                pr = adapter.get_pr(pr_num)
                if not pr:
                    continue
                logger.info("[E2E CLEANUP] Closing PR #%d: %s (label: %s)", pr_num, pr.title, label)
                adapter.close_pr(pr_num)
                branch = pr.branch or ""
                if branch:
                    try:
                        adapter.delete_branch(branch)
                    except Exception:
                        pass
                    branches_attempted.append(branch)
                closed_pr_nums.add(pr_num)
        except Exception as exc:
            logger.warning("[E2E CLEANUP] Failed listing PRs for label '%s': %s", label, exc)

    # Second, clean up OPEN PRs with e2e branch patterns
    try:
        prs = adapter.list_prs(state="open", limit=100)
        for pr in prs:
            pr_num = pr.number
            branch = pr.branch or ""
            if not pr_num or pr_num in closed_pr_nums:
                continue
            if any(pattern in branch.lower() for pattern in e2e_branch_patterns):
                logger.info("[E2E CLEANUP] Closing PR #%d: %s (branch pattern)", pr_num, pr.title)
                adapter.close_pr(pr_num)
                if branch:
                    try:
                        adapter.delete_branch(branch)
                    except Exception:
                        pass
                    branches_attempted.append(branch)
                closed_pr_nums.add(pr_num)
    except Exception as exc:
        logger.warning("[E2E CLEANUP] Failed to list open PRs: %s", exc)

    # Third, clean up branches from CLOSED/MERGED PRs that match e2e patterns
    try:
        prs = adapter.list_prs(state="all", limit=100)
        for pr in prs:
            if pr.state.lower() in ("closed", "merged"):
                branch = pr.branch or ""
                if any(pattern in branch.lower() for pattern in e2e_branch_patterns):
                    try:
                        adapter.delete_branch(branch)
                        branches_attempted.append(branch)
                        logger.info(
                            "[E2E CLEANUP] Deleted orphan branch: %s (from closed PR #%d)",
                            branch,
                            pr.number,
                        )
                    except Exception:
                        pass
    except Exception as exc:
        logger.warning("[E2E CLEANUP] Failed to list PRs for branch cleanup: %s", exc)

    pr_numbers = list(closed_pr_nums)

    def _pr_closed(pr_number: int) -> bool:
        try:
            pr = adapter.get_pr(pr_number)
            if not pr:
                return True
            return pr.state.upper() in {"CLOSED", "MERGED"}
        except Exception:
            return True

    def _branch_gone(branch: str) -> bool:
        try:
            return not adapter.branch_exists(branch)
        except Exception:
            return True

    verify_cleanup_items("PR cleanup", pr_numbers, _pr_closed, retries=1, retry_delay_s=3.0)
    verify_cleanup_items("PR branch cleanup", branches_attempted, _branch_gone, retries=1, retry_delay_s=3.0)

    return len(closed_pr_nums)


def ensure_pr_label(repo: str, label: str) -> None:
    """Ensure a PR label exists (noop if already created)."""
    try:
        _github_adapter(repo).create_label(label, force=True)
    except Exception:
        logger.warning("[E2E CLEANUP] Failed ensuring label: %s", label)


def ensure_required_pr_labels(repo: str) -> None:
    """Ensure required PR labels exist for e2e workflows."""
    labels = [
        "needs-code-review",
        "code-reviewed",
        "needs-rework",
        "rework-cycle-1",
        "rework-cycle-2",
        "triage-reviewed",
        "agent:triage-investigator",
        "agent:script-review",
        "agent:script-completes",
        "agent:e2e-test",
    ]
    for label in labels:
        ensure_pr_label(repo, label)


def cleanup_e2e_labels(repo: str, prefixes: tuple[str, ...]) -> int:
    """Delete e2e test labels that accumulate over time."""
    adapter = _github_adapter(repo)
    deleted = 0
    try:
        all_labels = adapter.list_all_labels()
        for label_data in all_labels:
            name = label_data.get("name", "")
            if any(name.startswith(prefix) for prefix in prefixes):
                try:
                    adapter._client.delete_label(name)
                    deleted += 1
                    logger.debug("[E2E CLEANUP] Deleted label: %s", name)
                except Exception:
                    pass
    except Exception as e:
        logger.warning("[E2E CLEANUP] Failed listing labels: %s", e)
    if deleted > 0:
        logger.info("[E2E CLEANUP] Deleted %d e2e test labels", deleted)
    return deleted


def cleanup_issues(repo: str) -> int:
    """Close test issues with test-data label."""
    adapter = _github_adapter(repo)
    try:
        issues = adapter.list_issues(labels=[DEFAULT_E2E_FILTER_LABEL], state="open", limit=100)
    except Exception:
        return 0
    closed_issues: list[int] = []
    for issue in issues:
        logger.info("[E2E CLEANUP] Closing issue #%d: %s", issue.number, issue.title)
        try:
            adapter.update_issue_state(issue.number, "closed")
            closed_issues.append(issue.number)
        except Exception:
            logger.warning("[E2E CLEANUP] Timeout closing issue #%d", issue.number)

    def _issue_closed(issue_number: int) -> bool:
        try:
            issue = adapter.get_issue(issue_number)
            if not issue:
                return True
            return issue.state.upper() == "CLOSED"
        except Exception:
            return True

    verify_cleanup_items("Issue cleanup", closed_issues, _issue_closed, retries=1, retry_delay_s=3.0)
    return len(issues)


__all__ = [
    "DEFAULT_E2E_FILTER_LABEL",
    "cleanup_local_worktrees",
    "cleanup_tmux_sessions",
    "run_cleanup_step",
    "verify_cleanup_items",
    "cleanup_remote_branches",
    "cleanup_prs",
    "ensure_pr_label",
    "ensure_required_pr_labels",
    "cleanup_e2e_labels",
    "cleanup_issues",
    "_keep_artifacts",
    "_keep_remote_artifacts",
]
