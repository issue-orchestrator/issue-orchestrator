"""Manage a persistent git worktree for E2E test isolation.

E2E tests run from their own worktree so fixtures that delete
``.issue-orchestrator/state/`` cannot destroy the live orchestrator's
SQLite files.
"""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def get_e2e_worktree_path(repo_root: Path) -> Path:
    """Derive the deterministic sibling path for the E2E worktree.

    Convention mirrors CLAUDE.md worktree naming:
    ``<repo_root>/../<repo_name>-e2e-worktree/``
    """
    return repo_root.parent / f"{repo_root.name}-e2e-worktree"


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command, raising on failure."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )


def _create_worktree(repo_root: Path, worktree_path: Path) -> None:
    """Create a new detached worktree at *worktree_path*."""
    logger.info("Creating E2E worktree at %s", worktree_path)
    _run_git(
        ["worktree", "add", "--detach", str(worktree_path), "origin/main"],
        cwd=repo_root,
    )


def _update_worktree(worktree_path: Path) -> None:
    """Reset an existing worktree to origin/main, preserving .venv."""
    logger.info("Updating E2E worktree at %s", worktree_path)
    _run_git(["checkout", "--detach", "origin/main"], cwd=worktree_path)
    _run_git(
        ["clean", "-fdx", "--exclude=.venv"],
        cwd=worktree_path,
    )


def _sync_venv(worktree_path: Path) -> None:
    """Ensure the worktree venv is up-to-date (fast when deps unchanged)."""
    logger.info("Syncing venv in E2E worktree")
    subprocess.run(
        ["uv", "sync", "--frozen", "--all-extras"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )


def _recover_worktree(repo_root: Path, worktree_path: Path) -> None:
    """Remove a broken worktree and recreate it."""
    logger.warning("Recovering E2E worktree at %s", worktree_path)
    try:
        _run_git(
            ["worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_root,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    _create_worktree(repo_root, worktree_path)


def ensure_e2e_worktree(repo_root: Path) -> Path:
    """Return a ready-to-use E2E worktree, creating or updating as needed.

    The worktree is a sibling directory checked out at ``origin/main``
    with a synced venv.  On failure a ``RuntimeError`` is raised
    (fail-fast per codebase design).

    Returns:
        Resolved ``Path`` to the worktree root.
    """
    worktree_path = get_e2e_worktree_path(repo_root)

    try:
        if worktree_path.exists():
            try:
                _update_worktree(worktree_path)
            except subprocess.CalledProcessError:
                _recover_worktree(repo_root, worktree_path)
        else:
            _create_worktree(repo_root, worktree_path)

        _sync_venv(worktree_path)

    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Failed to prepare E2E worktree at {worktree_path}: {exc.stderr}"
        ) from exc
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Required tool not found while preparing E2E worktree: {exc}"
        ) from exc

    logger.info("E2E worktree ready at %s", worktree_path)
    return worktree_path
