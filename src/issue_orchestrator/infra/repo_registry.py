"""Multi-repo registry for the supervisor.

Persists a list of registered repositories in ~/.config/issue-orchestrator/repos.json.
The supervisor can manage orchestrators for any registered repo.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _config_dir() -> Path:
    """Get the config directory for issue-orchestrator.

    Uses XDG_CONFIG_HOME if set, otherwise ~/.config.
    """
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        base = Path(xdg_config)
    else:
        base = Path.home() / ".config"
    return base / "issue-orchestrator"


def _repos_file() -> Path:
    """Get the path to the repos registry file."""
    return _config_dir() / "repos.json"


@dataclass
class RegisteredRepo:
    """A registered repository."""

    path: str
    name: str = ""
    added_at: str = ""

    def __post_init__(self) -> None:
        if not self.added_at:
            self.added_at = datetime.now(timezone.utc).isoformat()
        if not self.name:
            # Default name is the directory name
            self.name = Path(self.path).name

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "path": self.path,
            "name": self.name,
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegisteredRepo:
        """Create from dict."""
        return cls(
            path=data["path"],
            name=data.get("name", ""),
            added_at=data.get("added_at", ""),
        )


@dataclass
class RepoRegistry:
    """Registry of all managed repositories."""

    repos: list[RegisteredRepo] = field(default_factory=list)

    def add(self, repo_path: str | Path) -> RegisteredRepo:
        """Add a repository to the registry.

        Args:
            repo_path: Path to the repository root

        Returns:
            The registered repo entry

        Raises:
            ValueError: If the repo is already registered
        """
        normalized = str(Path(repo_path).resolve())

        # Check if already registered
        for repo in self.repos:
            if repo.path == normalized:
                raise ValueError(f"Repository already registered: {normalized}")

        repo = RegisteredRepo(path=normalized)
        self.repos.append(repo)
        return repo

    def remove(self, repo_path: str | Path) -> bool:
        """Remove a repository from the registry.

        Args:
            repo_path: Path to the repository root

        Returns:
            True if removed, False if not found
        """
        normalized = str(Path(repo_path).resolve())

        for i, repo in enumerate(self.repos):
            if repo.path == normalized:
                self.repos.pop(i)
                return True
        return False

    def get(self, repo_path: str | Path) -> RegisteredRepo | None:
        """Get a registered repo by path.

        Args:
            repo_path: Path to the repository root

        Returns:
            The registered repo or None if not found
        """
        normalized = str(Path(repo_path).resolve())

        for repo in self.repos:
            if repo.path == normalized:
                return repo
        return None

    def list_all(self) -> list[RegisteredRepo]:
        """Get all registered repos."""
        return list(self.repos)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "repos": [r.to_dict() for r in self.repos],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepoRegistry:
        """Create from dict."""
        repos = [RegisteredRepo.from_dict(r) for r in data.get("repos", [])]
        return cls(repos=repos)


def load_registry() -> RepoRegistry:
    """Load the repo registry from disk.

    Returns:
        The repo registry (empty if file doesn't exist)
    """
    path = _repos_file()
    if not path.exists():
        return RepoRegistry()

    try:
        with open(path) as f:
            data = json.load(f)
        return RepoRegistry.from_dict(data)
    except (json.JSONDecodeError, OSError):
        return RepoRegistry()


def save_registry(registry: RepoRegistry) -> None:
    """Save the repo registry to disk.

    Args:
        registry: The registry to save
    """
    path = _repos_file()
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w") as f:
        json.dump(registry.to_dict(), f, indent=2)


def add_repo(repo_path: str | Path) -> RegisteredRepo:
    """Add a repository to the registry.

    Convenience function that loads, adds, and saves.

    Args:
        repo_path: Path to the repository root

    Returns:
        The registered repo entry
    """
    registry = load_registry()
    repo = registry.add(repo_path)
    save_registry(registry)
    return repo


def remove_repo(repo_path: str | Path) -> bool:
    """Remove a repository from the registry.

    Convenience function that loads, removes, and saves.

    Args:
        repo_path: Path to the repository root

    Returns:
        True if removed, False if not found
    """
    registry = load_registry()
    removed = registry.remove(repo_path)
    if removed:
        save_registry(registry)
    return removed


def list_repos() -> list[RegisteredRepo]:
    """List all registered repositories.

    Returns:
        List of registered repos
    """
    return load_registry().list_all()
