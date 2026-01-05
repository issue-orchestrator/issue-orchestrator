"""Tests for repo_registry module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from issue_orchestrator.infra.repo_registry import (
    RegisteredRepo,
    RepoRegistry,
    add_repo,
    list_repos,
    load_registry,
    remove_repo,
    save_registry,
    _config_dir,
    _repos_file,
)


class TestConfigDir:
    """Tests for config directory resolution."""

    def test_uses_xdg_config_home_if_set(self, tmp_path: Path) -> None:
        """Uses XDG_CONFIG_HOME when set."""
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
            result = _config_dir()

        assert result == tmp_path / "issue-orchestrator"

    def test_uses_home_config_as_default(self) -> None:
        """Falls back to ~/.config when XDG_CONFIG_HOME not set."""
        with patch.dict("os.environ", {}, clear=True):
            with patch("os.environ.get", return_value=None):
                result = _config_dir()

        assert result == Path.home() / ".config" / "issue-orchestrator"


class TestRegisteredRepo:
    """Tests for RegisteredRepo dataclass."""

    def test_sets_default_name_from_path(self) -> None:
        """Default name is the directory name."""
        repo = RegisteredRepo(path="/home/user/projects/my-repo")

        assert repo.name == "my-repo"

    def test_sets_default_timestamp(self) -> None:
        """Timestamp is set on creation."""
        repo = RegisteredRepo(path="/home/user/projects/my-repo")

        assert repo.added_at
        assert "T" in repo.added_at  # ISO format

    def test_preserves_explicit_name(self) -> None:
        """Explicit name is preserved."""
        repo = RegisteredRepo(path="/home/user/projects/my-repo", name="Custom Name")

        assert repo.name == "Custom Name"

    def test_to_dict(self) -> None:
        """Converts to dict correctly."""
        repo = RegisteredRepo(
            path="/home/user/projects/my-repo",
            name="My Repo",
            added_at="2024-01-01T00:00:00+00:00",
        )

        result = repo.to_dict()

        assert result == {
            "path": "/home/user/projects/my-repo",
            "name": "My Repo",
            "added_at": "2024-01-01T00:00:00+00:00",
        }

    def test_from_dict(self) -> None:
        """Creates from dict correctly."""
        data = {
            "path": "/home/user/projects/my-repo",
            "name": "My Repo",
            "added_at": "2024-01-01T00:00:00+00:00",
        }

        repo = RegisteredRepo.from_dict(data)

        assert repo.path == "/home/user/projects/my-repo"
        assert repo.name == "My Repo"
        assert repo.added_at == "2024-01-01T00:00:00+00:00"

    def test_from_dict_with_minimal_data(self) -> None:
        """Creates from dict with only required fields."""
        data = {"path": "/home/user/projects/my-repo"}

        repo = RegisteredRepo.from_dict(data)

        assert repo.path == "/home/user/projects/my-repo"
        assert repo.name == "my-repo"  # Default from path
        assert repo.added_at  # Generated


class TestRepoRegistry:
    """Tests for RepoRegistry class."""

    def test_add_repo(self, tmp_path: Path) -> None:
        """Adding a repo works."""
        registry = RepoRegistry()
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()

        result = registry.add(repo_path)

        assert result.path == str(repo_path.resolve())
        assert len(registry.repos) == 1

    def test_add_duplicate_raises(self, tmp_path: Path) -> None:
        """Adding a duplicate raises ValueError."""
        registry = RepoRegistry()
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()
        registry.add(repo_path)

        with pytest.raises(ValueError, match="already registered"):
            registry.add(repo_path)

    def test_remove_repo(self, tmp_path: Path) -> None:
        """Removing a repo works."""
        registry = RepoRegistry()
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()
        registry.add(repo_path)

        result = registry.remove(repo_path)

        assert result is True
        assert len(registry.repos) == 0

    def test_remove_nonexistent_returns_false(self, tmp_path: Path) -> None:
        """Removing a non-existent repo returns False."""
        registry = RepoRegistry()

        result = registry.remove(tmp_path / "nonexistent")

        assert result is False

    def test_get_repo(self, tmp_path: Path) -> None:
        """Getting a repo by path works."""
        registry = RepoRegistry()
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()
        registry.add(repo_path)

        result = registry.get(repo_path)

        assert result is not None
        assert result.path == str(repo_path.resolve())

    def test_get_nonexistent_returns_none(self, tmp_path: Path) -> None:
        """Getting a non-existent repo returns None."""
        registry = RepoRegistry()

        result = registry.get(tmp_path / "nonexistent")

        assert result is None

    def test_list_all(self, tmp_path: Path) -> None:
        """Listing all repos works."""
        registry = RepoRegistry()
        (tmp_path / "repo1").mkdir()
        (tmp_path / "repo2").mkdir()
        registry.add(tmp_path / "repo1")
        registry.add(tmp_path / "repo2")

        result = registry.list_all()

        assert len(result) == 2

    def test_to_dict(self, tmp_path: Path) -> None:
        """Converting to dict works."""
        registry = RepoRegistry()
        (tmp_path / "repo1").mkdir()
        registry.add(tmp_path / "repo1")

        result = registry.to_dict()

        assert "repos" in result
        assert len(result["repos"]) == 1

    def test_from_dict(self) -> None:
        """Creating from dict works."""
        data = {
            "repos": [
                {
                    "path": "/home/user/repo1",
                    "name": "Repo 1",
                    "added_at": "2024-01-01T00:00:00+00:00",
                }
            ]
        }

        registry = RepoRegistry.from_dict(data)

        assert len(registry.repos) == 1
        assert registry.repos[0].name == "Repo 1"


class TestLoadSaveRegistry:
    """Tests for load/save functions."""

    def test_load_empty_when_file_missing(self, tmp_path: Path) -> None:
        """Returns empty registry when file doesn't exist."""
        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=tmp_path / "nonexistent.json",
        ):
            registry = load_registry()

        assert len(registry.repos) == 0

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        """Save creates config directory if needed."""
        repos_file = tmp_path / "config" / "issue-orchestrator" / "repos.json"
        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            registry = RepoRegistry()
            registry.repos.append(
                RegisteredRepo(path="/home/user/repo", name="Test")
            )

            save_registry(registry)

        assert repos_file.exists()

    def test_roundtrip(self, tmp_path: Path) -> None:
        """Save then load preserves data."""
        repos_file = tmp_path / "repos.json"
        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            registry = RepoRegistry()
            registry.repos.append(
                RegisteredRepo(
                    path="/home/user/repo",
                    name="Test Repo",
                    added_at="2024-01-01T00:00:00+00:00",
                )
            )
            save_registry(registry)

            loaded = load_registry()

        assert len(loaded.repos) == 1
        assert loaded.repos[0].path == "/home/user/repo"
        assert loaded.repos[0].name == "Test Repo"

    def test_load_handles_corrupt_json(self, tmp_path: Path) -> None:
        """Returns empty registry for corrupt JSON."""
        repos_file = tmp_path / "repos.json"
        repos_file.write_text("not valid json{{{")

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            registry = load_registry()

        assert len(registry.repos) == 0


class TestConvenienceFunctions:
    """Tests for add_repo, remove_repo, list_repos."""

    def test_add_repo_saves(self, tmp_path: Path) -> None:
        """add_repo saves to disk."""
        repos_file = tmp_path / "repos.json"
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            result = add_repo(repo_path)

        assert result.path == str(repo_path.resolve())
        assert repos_file.exists()

        # Verify persisted
        data = json.loads(repos_file.read_text())
        assert len(data["repos"]) == 1

    def test_remove_repo_saves(self, tmp_path: Path) -> None:
        """remove_repo saves to disk."""
        repos_file = tmp_path / "repos.json"
        repo_path = tmp_path / "my-repo"
        repo_path.mkdir()

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            add_repo(repo_path)
            result = remove_repo(repo_path)

        assert result is True

        # Verify persisted
        data = json.loads(repos_file.read_text())
        assert len(data["repos"]) == 0

    def test_list_repos(self, tmp_path: Path) -> None:
        """list_repos returns all repos."""
        repos_file = tmp_path / "repos.json"
        (tmp_path / "repo1").mkdir()
        (tmp_path / "repo2").mkdir()

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=repos_file,
        ):
            add_repo(tmp_path / "repo1")
            add_repo(tmp_path / "repo2")

            result = list_repos()

        assert len(result) == 2
