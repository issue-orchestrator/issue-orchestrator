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

    Checks ISSUE_ORCHESTRATOR_CONFIG_DIR first (for testing),
    then XDG_CONFIG_HOME, otherwise ~/.config.
    """
    # Allow override for testing - isolates test registry from production
    if override := os.environ.get("ISSUE_ORCHESTRATOR_CONFIG_DIR"):
        return Path(override)
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
class RepoHealth:
    """Health status of a repository."""

    status: str  # "valid", "invalid", "unknown"
    checked_at: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.checked_at:
            self.checked_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "status": self.status,
            "checked_at": self.checked_at,
            "errors": self.errors,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepoHealth:
        """Create from dict."""
        return cls(
            status=data.get("status", "unknown"),
            checked_at=data.get("checked_at", ""),
            errors=data.get("errors", []),
            warnings=data.get("warnings", []),
        )


@dataclass
class RegisteredRepo:
    """A registered repository."""

    path: str
    name: str = ""
    added_at: str = ""
    health: RepoHealth | None = None  # Cached health status
    selected_config: str = "default.yaml"  # Last used config file

    def __post_init__(self) -> None:
        if not self.added_at:
            self.added_at = datetime.now(timezone.utc).isoformat()
        if not self.name:
            # Default name is the directory name
            self.name = Path(self.path).name

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        result: dict[str, Any] = {
            "path": self.path,
            "name": self.name,
            "added_at": self.added_at,
            "selected_config": self.selected_config,
        }
        if self.health:
            result["health"] = self.health.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegisteredRepo:
        """Create from dict."""
        health = None
        if "health" in data:
            health = RepoHealth.from_dict(data["health"])
        return cls(
            path=data["path"],
            name=data.get("name", ""),
            added_at=data.get("added_at", ""),
            health=health,
            selected_config=data.get("selected_config", "default.yaml"),
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


def check_repo_health(repo_path: str | Path, config_name: str = "default.yaml") -> RepoHealth:
    """Run doctor checks for a repository and return health status.

    Args:
        repo_path: Path to the repository root
        config_name: Name of config file to check (default: default.yaml)

    Returns:
        RepoHealth with status, errors, and warnings
    """
    from .doctor import run_doctor
    from .config import Config, get_config_path, list_configs
    from ..execution.command_runner import LocalCommandRunner

    repo_path = Path(repo_path)

    # Check if any configs exist
    available_configs = list_configs(repo_path)
    if not available_configs:
        return RepoHealth(
            status="invalid",
            errors=["No configuration found. Run the setup wizard to create one."],
        )

    # Try to load the specified config
    config = None
    config_path = get_config_path(repo_path, config_name)

    if config_path.exists():
        try:
            config = Config.load(config_path)
        except Exception as e:
            return RepoHealth(
                status="invalid",
                errors=[f"Failed to load config: {e}"],
            )
    else:
        return RepoHealth(
            status="invalid",
            errors=[f"Config file not found: {config_path}"],
        )

    # Run doctor (change to repo directory for worktree checks)
    import os
    original_cwd = os.getcwd()
    try:
        os.chdir(repo_path)
        result = run_doctor(config=config, config_path=config_path, runner=LocalCommandRunner())
    finally:
        os.chdir(original_cwd)

    # Convert to RepoHealth
    errors = [f"{c.name}: {c.detail}" for c in result.checks if c.status == "error"]
    warnings = [f"{c.name}: {c.detail}" for c in result.checks if c.status == "warning"]

    if result.overall == "error":
        status = "invalid"
    elif result.overall == "warning":
        status = "valid"  # Warnings don't block, just notify
    else:
        status = "valid"

    return RepoHealth(status=status, errors=errors, warnings=warnings)


def update_repo_health(repo_path: str | Path, config_name: str | None = None) -> RepoHealth:
    """Run doctor checks and persist the health status.

    Args:
        repo_path: Path to the repository root
        config_name: Config file to check (uses selected_config if not provided)

    Returns:
        The updated RepoHealth
    """
    normalized = str(Path(repo_path).resolve())
    registry = load_registry()

    # Find the repo
    for repo in registry.repos:
        if repo.path == normalized:
            # Use provided config_name or fall back to selected_config
            cfg_name = config_name or repo.selected_config
            # Run health check
            health = check_repo_health(repo_path, cfg_name)
            repo.health = health
            save_registry(registry)
            return health

    # Repo not found - run check anyway but don't persist
    return check_repo_health(repo_path, config_name or "default.yaml")


def set_selected_config(repo_path: str | Path, config_name: str) -> bool:
    """Set the selected config for a repository.

    Args:
        repo_path: Path to the repository root
        config_name: Config file name to select

    Returns:
        True if updated, False if repo not found
    """
    normalized = str(Path(repo_path).resolve())
    registry = load_registry()

    for repo in registry.repos:
        if repo.path == normalized:
            repo.selected_config = config_name
            save_registry(registry)
            return True
    return False
