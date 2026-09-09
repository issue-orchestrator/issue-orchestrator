"""Configured-repository adapter for Control Center recovery queries."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from ..domain.control_center_recovery import ConfiguredRepository
from ..infra.repo_identity import configured_repository_key, normalize_repo_root


class RegisteredRepository(Protocol):
    path: str
    selected_config: str
    selected_mode: str


RepositoryLoader = Callable[[], Sequence[RegisteredRepository]]
RepositorySlugLoader = Callable[[RegisteredRepository], str]


def _registered_repositories() -> Sequence[RegisteredRepository]:
    from ..infra.repo_registry import list_repos

    return list_repos()


def _configured_repo_slug(repository: RegisteredRepository) -> str:
    from ..infra.config import Config, get_config_path

    path = get_config_path(
        Path(repository.path),
        repository.selected_config,
        repository.selected_mode,
    )
    repo_slug = Config.load(path).repo
    if type(repo_slug) is not str or not repo_slug.strip():
        raise ValueError(f"Configured repository slug is missing from {path}")
    return repo_slug


class RegisteredConfiguredRepositoryRegistry:
    """Resolve only roots already admitted to the Control Center registry."""

    def __init__(
        self,
        *,
        repositories: RepositoryLoader = _registered_repositories,
        repo_slug: RepositorySlugLoader = _configured_repo_slug,
    ) -> None:
        self._repositories = repositories
        self._repo_slug = repo_slug

    def resolve(self, repo_key: str) -> ConfiguredRepository | None:
        if type(repo_key) is not str or not repo_key.startswith("repo-"):
            return None
        for registered in self._repositories():
            root = str(normalize_repo_root(registered.path))
            if configured_repository_key(root) != repo_key:
                continue
            return ConfiguredRepository(
                repo_key=repo_key,
                repo_root=root,
                repo_slug=self._repo_slug(registered),
            )
        return None


__all__ = [
    "RegisteredConfiguredRepositoryRegistry",
    "configured_repository_key",
]
