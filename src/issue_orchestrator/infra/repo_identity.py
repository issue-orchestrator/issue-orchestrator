"""Repository identity utilities.

Provides canonical path resolution and state directory management for
per-repository orchestrator instances.
"""

from pathlib import Path
from typing import Optional


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


def _resolve_git_dir(repo_path: Path) -> Optional[Path]:
    git_path = repo_path / ".git"
    if git_path.is_dir():
        return git_path
    if git_path.is_file():
        content = git_path.read_text().strip()
        if content.startswith("gitdir:"):
            gitdir = content.split("gitdir:", 1)[1].strip()
            gitdir_path = Path(gitdir)
            if not gitdir_path.is_absolute():
                gitdir_path = (repo_path / gitdir_path).resolve()
            return gitdir_path
    return None


def get_repo_head_sha(repo_root: Path | str) -> Optional[str]:
    """Return the current HEAD commit SHA for a repo without invoking git."""
    repo_path = normalize_repo_root(repo_root)
    git_dir = _resolve_git_dir(repo_path)
    if git_dir is None:
        return None
    head_path = git_dir / "HEAD"
    if not head_path.exists():
        return None
    head = head_path.read_text().strip()
    if head.startswith("ref: "):
        ref = head.split("ref: ", 1)[1].strip()
        ref_path = git_dir / ref
        if ref_path.exists():
            return ref_path.read_text().strip() or None
        packed = git_dir / "packed-refs"
        if packed.exists():
            for line in packed.read_text().splitlines():
                if line.startswith("#") or line.startswith("^") or not line.strip():
                    continue
                sha, name = line.split(" ", 1)
                if name.strip() == ref:
                    return sha.strip() or None
        return None
    return head or None
