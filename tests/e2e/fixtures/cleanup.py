"""E2E cleanup functions for test artifacts."""

import logging
import subprocess
import threading
import time
from pathlib import Path

from issue_orchestrator.adapters.worktree.custody import (
    GitMetadataWorktreeCustody,
    git_common_dir,
)
from issue_orchestrator.adapters.worktree.removal import (
    remove_checkout_path,
    remove_empty_worktree_container,
)
from issue_orchestrator.ports.worktree_custody import (
    CustodyError,
    CustodyUnavailableError,
)

from .github_client import _github_adapter
from .orchestrator_process import keep_artifacts, keep_remote_artifacts

logger = logging.getLogger(__name__)

# Explicit label for e2e test artifacts - specific to avoid accidental cleanup
DEFAULT_E2E_FILTER_LABEL = "io-e2e-test-data"


def cleanup_local_worktrees(
    worktree_base: Path | None = None, *, repo_root: Path
) -> int:
    """Clean up local e2e worktrees, through the one removal owner.

    A checkout retained from a failed run is exactly the thing an operator
    holds, and the next session starting is exactly when they lose it (#7274).
    Earlier versions entered ``custody_guard`` here and did their own
    ``shutil.rmtree``, which made this a SECOND removal owner -- one the guard
    test does not read (round 7 finding 1).

    It sweeps CHECKOUTS, not whatever directories happen to sit under the base.
    E2E runs with ``ORCHESTRATOR_WORKTREE_PER_SESSION=1``, so the real layout is
    ``<base>/<session>/<checkout>``: handing the session container to the owner
    asked about custody on a directory no grant names, and the forced fallback
    then deleted the subtree with the held checkout inside it (round 8
    finding 1). Containers are emptied by removing their checkouts and then
    removed with a non-recursive ``rmdir``, which cannot take anything with it.

    Args:
        worktree_base: Base directory for worktrees. Defaults to
            /tmp/e2e-worktrees.
        repo_root: Which repository to ask about custody. Required and
            keyword-only, with no default, because one of these checkouts can
            have lost its own ``.git`` file and custody lives in the
            REPOSITORY: a caller that simply omitted it would delete a held
            checkout while believing it had asked.
    """
    if worktree_base is None:
        worktree_base = Path("/tmp/e2e-worktrees")
    if not worktree_base.exists():
        return 0
    checkouts, containers = _sweep_targets(worktree_base, repo_root=repo_root)
    count = 0
    for checkout in checkouts:
        try:
            outcome = remove_checkout_path(
                checkout,
                force=True,
                run_git=_sweep_git(repo_root),
                repo_root=repo_root,
            )
        except CustodyError as e:
            logger.warning("Retaining held worktree %s: %s", checkout, e)
            continue
        except Exception as e:
            logger.warning("Failed to remove worktree %s: %s", checkout, e)
            continue
        if outcome.removed:
            count += 1
        else:
            logger.warning(
                "Failed to remove worktree %s: %s", checkout, outcome.git_error
            )
    _remove_empty_containers(containers, repo_root=repo_root)
    if count > 0:
        logger.info(
            "[E2E CLEANUP] Removed %d local worktrees from %s", count, worktree_base
        )
    return 0


def _sweep_targets(
    worktree_base: Path, *, repo_root: Path
) -> tuple[list[Path], list[Path]]:
    """Checkouts PROVEN to belong to this repository, and their containers.

    A missing ``.git`` entry cannot establish ownership. The default base is
    shared across runs, so asking repository B about an unregistered checkout
    from repository A gives a truthful answer about the WRONG store -- and
    deletes A's held checkout (round 15 finding 2). Such a path is swept only
    when this repository's own custody store already names it.
    """
    repo_common = git_common_dir(repo_root)
    if repo_common is None:
        raise CustodyUnavailableError(
            f"{repo_root} is not a repository, so E2E cleanup cannot establish "
            "which checkouts it owns"
        )
    held = {
        grant.path.resolve()
        for grant in GitMetadataWorktreeCustody(repo_common).list_held()
    }

    def belongs_to_repository(candidate: Path) -> bool:
        if candidate.resolve() in held:
            return True
        try:
            return git_common_dir(candidate) == repo_common
        except CustodyError:
            # Damaged identity is not authority to reinterpret the directory as
            # a session container and descend into it.
            return False

    checkouts: list[Path] = []
    containers: list[Path] = []
    for item in sorted(worktree_base.iterdir()):
        if not item.is_dir():
            continue
        if belongs_to_repository(item):
            checkouts.append(item)
            continue
        children = [
            child
            for child in sorted(item.iterdir())
            if child.is_dir() and belongs_to_repository(child)
        ]
        if children:
            containers.append(item)
            checkouts.extend(children)
    return checkouts, containers


def _remove_empty_containers(containers: list[Path], *, repo_root: Path) -> None:
    """Drop only containers proven by their repository-owned children.

    ``rmdir`` and not ``rmtree`` on purpose: a container that still holds
    something holds a checkout this sweep RETAINED, and the whole point is that
    removing the parent must not be a way around that.
    """
    for item in containers:
        if item.is_dir():
            # Through the removal owner, which asks custody first. An EMPTY held
            # checkout that lost its `.git` looks exactly like a container from
            # out here, and this was the one path that never asked (round 14
            # finding 2).
            try:
                if not remove_empty_worktree_container(item, repo_root=repo_root):
                    logger.info("Retaining non-empty worktree container %s", item)
            except CustodyError as e:
                logger.warning("Retaining held worktree container %s: %s", item, e)


def _sweep_git(repo_root: Path):
    """How the removal owner runs git for this sweep."""

    def run(argv: list[str]) -> "str | None":
        result = subprocess.run(
            ["git", "-C", str(repo_root), *argv], capture_output=True, text=True
        )
        return None if result.returncode == 0 else (result.stderr or "").strip()

    return run


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
    """Clean up remote branches from PRs with io-e2e-test-data label.

    Uses the branch info already present in PRInfo from get_prs_with_label
    to avoid N+1 individual get_pr() calls.
    """
    branches_deleted = 0
    branches_attempted: list[str] = []
    deadline = time.monotonic() + 30

    adapter = _github_adapter(repo)

    try:
        # get_prs_with_label returns PRInfo with branch already populated
        items = adapter.get_prs_with_label(DEFAULT_E2E_FILTER_LABEL, state="all")
        for item in items:
            if time.monotonic() > deadline:
                logger.warning("[E2E CLEANUP] Branch cleanup time budget exceeded; stopping early")
                return branches_deleted
            branch = item.branch or ""
            if branch:
                try:
                    adapter.delete_branch(branch)
                    branches_attempted.append(branch)
                    logger.info("[E2E CLEANUP] Deleted branch for PR #%d: %s", item.number, branch)
                    branches_deleted += 1
                except Exception:
                    logger.warning("[E2E CLEANUP] Failed deleting branch for PR #%s: %s", item.number, branch)
    except Exception as exc:
        logger.warning("[E2E CLEANUP] Failed to list PRs for branch cleanup: %s", exc)

    def _branch_gone(branch: str) -> bool:
        try:
            return not adapter.branch_exists(branch)
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
    """Clean up PRs with io-e2e-test-data label only.

    Fetches all PRs (open + closed) in a single query. Closes open ones
    and deletes branches for all of them.
    """
    closed_pr_nums: set[int] = set()
    branches_attempted: list[str] = []

    adapter = _github_adapter(repo)

    # Single query for all states — avoids two separate API calls
    try:
        items = adapter.get_prs_with_label(DEFAULT_E2E_FILTER_LABEL, state="all")
        for item in items:
            pr_num = item.number
            if not pr_num:
                continue
            # Close open PRs
            if item.state.lower() == "open":
                logger.info("[E2E CLEANUP] Closing PR #%d: %s (label: %s)", pr_num, item.title, DEFAULT_E2E_FILTER_LABEL)
                try:
                    adapter.close_pr(pr_num)
                    closed_pr_nums.add(pr_num)
                except Exception:
                    logger.warning("[E2E CLEANUP] Failed closing PR #%d", pr_num)
            # Delete branch for all PRs (open, closed, merged)
            branch = item.branch or ""
            if branch:
                try:
                    adapter.delete_branch(branch)
                    branches_attempted.append(branch)
                    if item.state.lower() != "open":
                        logger.info("[E2E CLEANUP] Deleted branch: %s (from %s PR #%d)", branch, item.state, pr_num)
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("[E2E CLEANUP] Failed listing PRs for label '%s': %s", DEFAULT_E2E_FILTER_LABEL, exc)

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
        "tech-lead-reviewed",
        "agent:tech-lead-investigator",
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
                    # noqa: SLF001 - E2E test infrastructure needs direct label cleanup
                    adapter._client.delete_label(name)  # noqa: SLF001
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
    """Close test issues with io-e2e-test-data label."""
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
    "run_cleanup_step",
    "verify_cleanup_items",
    "cleanup_remote_branches",
    "cleanup_prs",
    "ensure_pr_label",
    "ensure_required_pr_labels",
    "cleanup_e2e_labels",
    "cleanup_issues",
    "keep_artifacts",
    "keep_remote_artifacts",
]
