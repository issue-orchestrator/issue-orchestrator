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

from .artifact_cleanup import (
    E2E_ARTIFACT_LABEL,
    RUN_ISSUES,
    E2EArtifactCleanup,
    RunIssues,
)
from .github_client import _github_adapter
from .orchestrator_process import keep_artifacts, keep_remote_artifacts

logger = logging.getLogger(__name__)

# Explicit label for e2e test artifacts - specific to avoid accidental cleanup
DEFAULT_E2E_FILTER_LABEL = E2E_ARTIFACT_LABEL


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

    def candidate_common_dir(candidate: Path) -> "Path | None":
        """Repository identity, preserving the UNREADABLE third state.

        A bool collapsed "not a repository", "another repository" and "identity
        unreadable" into one answer, and the caller then descended into all
        three as though they were session containers -- which is the opposite of
        what the comment claimed (round 16 finding 2).
        """
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise CustodyUnavailableError(
                f"cannot resolve shared-base candidate {candidate}: {exc}"
            ) from exc
        if resolved in held:
            return repo_common
        return git_common_dir(candidate)

    checkouts: list[Path] = []
    containers: list[Path] = []
    for item in sorted(worktree_base.iterdir()):
        if item.is_symlink() or not item.is_dir():
            continue
        try:
            item_common = candidate_common_dir(item)
        except CustodyError as exc:
            logger.warning(
                "Retaining opaque worktree candidate %s without descending: %s",
                item,
                exc,
            )
            continue
        if item_common == repo_common:
            checkouts.append(item)
            continue
        if item_common is not None:
            # A checkout of ANOTHER repository is not one of our containers.
            continue
        children: list[Path] = []
        for child in sorted(item.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            try:
                child_common = candidate_common_dir(child)
            except CustodyError as exc:
                logger.warning("Retaining opaque worktree candidate %s: %s", child, exc)
                continue
            if child_common == repo_common:
                children.append(child)
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
            logger.warning("[E2E CLEANUP] %s failed", name, exc_info=True)
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


#: How an operator removes leftovers once no run is live (``tests/e2e/reconcile.py``).
RECONCILE_COMMAND = (
    "IO_E2E=1 E2E_TEST_REPO={repo} python -m tests.e2e.reconcile --no-live-run-against {repo}"
)


def report_leftovers(repo: str, run_issues: RunIssues = RUN_ISSUES) -> int:
    """Report the open e2e PRs and issues this run did not create; never touch them.

    Nothing this run can observe proves the run that made them has ended, so
    they are left to that run, or to the operator's explicit reconciliation
    (#9523). Returns how many there are.
    """
    cleanup = E2EArtifactCleanup.for_test_repo(repo)
    own = run_issues.scope()
    prs = [
        f"PR #{pr.number}" for pr in cleanup.open_prs_with_label(E2E_ARTIFACT_LABEL)
        if not own.admits_pr(pr, cleanup.repo)
    ]
    issues = [
        f"issue #{item.number}" for item in cleanup.open_issues_with_label(E2E_ARTIFACT_LABEL)
        if not own.admits_issue(item, cleanup.repo)
    ]
    leftovers = prs + issues
    if leftovers:
        logger.warning(
            "[E2E RECONCILIATION] %d open e2e artifact(s) left by other runs: %s. One "
            "may belong to a run still going, so this run leaves them (an open e2e "
            "issue is also work this run's orchestrator may pick up). Once no run is "
            "live against %s, remove them with: %s",
            len(leftovers),
            ", ".join(leftovers),
            repo,
            RECONCILE_COMMAND.format(repo=repo),
        )
    return len(leftovers)


def cleanup_run_prs(repo: str, run_issues: RunIssues = RUN_ISSUES) -> int:
    """Close the open e2e PRs of every issue this run recorded creating."""
    return E2EArtifactCleanup.for_test_repo(repo).sweep(run_issues.scope())


def close_run_issues(repo: str, run_issues: RunIssues = RUN_ISSUES) -> int:
    """Close every open issue this run recorded creating, and no other."""
    return E2EArtifactCleanup.for_test_repo(repo).sweep_issues(
        run_issues.scope(), comment="Closed by e2e cleanup of the run that created it."
    )


def close_run_issue(repo: str, issue_number: int, comment: str | None = None) -> None:
    """Close one issue this run recorded creating (refused for any other)."""
    E2EArtifactCleanup.for_test_repo(repo).close_issue(RUN_ISSUES.scope(), issue_number, comment)


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


__all__ = [
    "DEFAULT_E2E_FILTER_LABEL",
    "cleanup_local_worktrees",
    "run_cleanup_step",
    "verify_cleanup_items",
    "report_leftovers",
    "cleanup_run_prs",
    "close_run_issues",
    "close_run_issue",
    "ensure_pr_label",
    "ensure_required_pr_labels",
    "keep_artifacts",
    "keep_remote_artifacts",
]
