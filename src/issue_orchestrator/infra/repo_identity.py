"""Repository identity utilities.

Provides canonical path resolution and state directory management for
per-repository orchestrator instances.
"""

from pathlib import Path


def normalize_repo_root(path: Path | str) -> Path:
    """Normalize a repository root path to its canonical absolute form.

    Args:
        path: Repository root path (can be relative or contain symlinks)

    Returns:
        Canonical absolute path (resolved symlinks, normalized)
    """
    return Path(path).resolve()


def state_dir(repo_root: Path | str) -> Path:
    """Get the state directory for a repository.

    Args:
        repo_root: Repository root path

    Returns:
        Path to .issue-orchestrator/state directory
    """
    return normalize_repo_root(repo_root) / ".issue-orchestrator" / "state"


def lock_file(repo_root: Path | str) -> Path:
    """Get the lock file path for a repository.

    Args:
        repo_root: Repository root path

    Returns:
        Path to .issue-orchestrator/lock.json
    """
    return normalize_repo_root(repo_root) / ".issue-orchestrator" / "lock.json"
