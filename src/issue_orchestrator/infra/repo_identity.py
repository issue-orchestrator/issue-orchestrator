"""Repository identity utilities.

Provides canonical path resolution and state directory management for
per-repository orchestrator instances.
"""

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


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


def lock_file(repo_root: Path | str, instance_id: str | None = None) -> Path:
    """Get the lock file path for a repository (or specific instance).

    Args:
        repo_root: Repository root path
        instance_id: Optional instance ID for multi-instance deployments.
                    If None, returns the legacy single-instance lock path.

    Returns:
        Path to lock file:
        - Single instance: .issue-orchestrator/lock.json
        - Multi-instance: .issue-orchestrator/locks/{instance_id}.json
    """
    repo_root = normalize_repo_root(repo_root)
    if instance_id is None:
        return repo_root / ".issue-orchestrator" / "lock.json"
    return repo_root / ".issue-orchestrator" / "locks" / f"{instance_id}.json"


def locks_dir(repo_root: Path | str) -> Path:
    """Get the locks directory for multi-instance deployments.

    Args:
        repo_root: Repository root path

    Returns:
        Path to .issue-orchestrator/locks/
    """
    return normalize_repo_root(repo_root) / ".issue-orchestrator" / "locks"


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


@dataclass(frozen=True)
class RepoIdentity:
    """Canonical repository + source identity used by CC/engine handshake."""

    repo_root: str
    commit_sha: str | None
    branch: str | None
    working_tree_dirty: bool
    dirty_fingerprint: str | None
    source_root: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "working_tree_dirty": self.working_tree_dirty,
            "dirty_fingerprint": self.dirty_fingerprint,
            "source_root": self.source_root,
        }


def _run_git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"git {' '.join(args)} failed for {repo_root}: {stderr}")
    return result.stdout.strip()


def get_package_source_root() -> str | None:
    """Resolve source root where issue_orchestrator package is loaded from."""
    try:
        import issue_orchestrator  # local import to avoid import cycle at module load

        package_path = Path(issue_orchestrator.__file__).resolve()
        return str(package_path.parent.parent)
    except Exception:
        return None


def build_repo_identity(repo_root: Path | str) -> RepoIdentity:
    """Build deterministic repository identity for runtime handshake."""
    root = normalize_repo_root(repo_root)
    commit_sha = get_repo_head_sha(root)
    branch = None
    try:
        branch_output = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        branch = None if branch_output == "HEAD" else branch_output
    except Exception:
        branch = None

    try:
        status_output = _run_git(root, "status", "--porcelain=v1", "--untracked-files=normal")
        dirty_lines = [line.rstrip() for line in status_output.splitlines() if line.strip()]
    except Exception:
        dirty_lines = []
    dirty_fingerprint = None
    if dirty_lines:
        digest = hashlib.sha256("\n".join(dirty_lines).encode("utf-8")).hexdigest()
        dirty_fingerprint = digest[:16]

    return RepoIdentity(
        repo_root=str(root),
        commit_sha=commit_sha,
        branch=branch,
        working_tree_dirty=bool(dirty_lines),
        dirty_fingerprint=dirty_fingerprint,
        source_root=get_package_source_root(),
    )


def serialize_repo_identity(identity: RepoIdentity | dict[str, Any]) -> str:
    """Serialize identity for environment transport."""
    payload = identity.to_dict() if isinstance(identity, RepoIdentity) else identity
    return json.dumps(payload, sort_keys=True)


def deserialize_repo_identity(payload: str) -> RepoIdentity:
    """Deserialize identity from environment payload."""
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Expected object payload")
    return RepoIdentity(
        repo_root=str(data.get("repo_root", "")),
        commit_sha=(str(data["commit_sha"]) if data.get("commit_sha") else None),
        branch=(str(data["branch"]) if data.get("branch") else None),
        working_tree_dirty=bool(data.get("working_tree_dirty", False)),
        dirty_fingerprint=(str(data["dirty_fingerprint"]) if data.get("dirty_fingerprint") else None),
        source_root=(str(data["source_root"]) if data.get("source_root") else None),
    )


def diff_repo_identity(expected: RepoIdentity, observed: RepoIdentity) -> dict[str, dict[str, Any]]:
    """Return field-wise mismatch details."""
    mismatches: dict[str, dict[str, Any]] = {}
    for field in (
        "repo_root",
        "commit_sha",
        "branch",
        "working_tree_dirty",
        "dirty_fingerprint",
        "source_root",
    ):
        expected_value = getattr(expected, field)
        observed_value = getattr(observed, field)
        if expected_value != observed_value:
            mismatches[field] = {"expected": expected_value, "observed": observed_value}
    return mismatches
