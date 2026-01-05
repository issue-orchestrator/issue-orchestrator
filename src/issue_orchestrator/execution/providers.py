"""Provider functions for standalone entrypoints.

This module provides factory functions that return protocol implementations
without requiring a full orchestrator context. It's the composition root
for standalone CLI commands, wizards, and the control API.

Entrypoints should import from this module instead of importing adapters directly.
This keeps the layer boundary clean: entrypoints -> providers -> adapters.

Example:
    # Instead of:
    from ..adapters.github import GitHubAdapter
    adapter = GitHubAdapter(repo=repo)

    # Use:
    from ..execution.providers import create_repository_host
    host = create_repository_host(repo=repo)
"""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports import RepositoryHost


# =============================================================================
# GitHub Providers
# =============================================================================


def create_repository_host(repo: str) -> "RepositoryHost":
    """Create a RepositoryHost for the given repository.

    Args:
        repo: Repository in 'owner/repo' format

    Returns:
        A RepositoryHost implementation (GitHubAdapter)
    """
    from ..adapters.github import GitHubAdapter

    return GitHubAdapter(repo=repo)


def resolve_github_token(
    configured_token: str | None = None,
    configured_env: str | None = None,
) -> str:
    """Resolve GitHub token from various sources.

    Checks in order:
    1. Explicitly configured token
    2. Environment variable (configured or GITHUB_TOKEN)
    3. Keyring storage
    4. gh CLI auth

    Args:
        configured_token: Explicitly provided token
        configured_env: Environment variable name to check

    Returns:
        GitHub token string

    Raises:
        ValueError: If no token found
    """
    from ..adapters.github.http_client import resolve_github_token as _resolve

    return _resolve(configured_token=configured_token, configured_env=configured_env)


def store_keyring_token(token: str) -> None:
    """Store GitHub token in system keyring.

    Args:
        token: GitHub token to store
    """
    from ..adapters.github.http_client import store_keyring_token as _store

    _store(token)


def clear_keyring_token() -> None:
    """Clear GitHub token from system keyring."""
    from ..adapters.github.http_client import clear_keyring_token as _clear

    _clear()


def get_repo_from_git() -> str | None:
    """Detect GitHub repository from git remote.

    Returns:
        Repository in 'owner/repo' format, or None if not detected
    """
    from ..adapters.github.repo import get_repo_from_git as _get_repo
    from ..adapters.github.repo import GitRepoError

    try:
        return _get_repo()
    except GitRepoError:
        return None


# =============================================================================
# Terminal Providers (tmux)
# =============================================================================


def get_tmux_manager() -> "TmuxManager":
    """Get the tmux session manager.

    Returns:
        TmuxManager instance for managing tmux sessions
    """
    from ..adapters.terminal._tmux import get_manager

    return get_manager()


def list_tmux_sessions() -> list[str]:
    """List all tmux sessions.

    Returns:
        List of tmux session names
    """
    from ..adapters.terminal._tmux import list_sessions

    return list_sessions()


def attach_tmux_session(session_name: str) -> None:
    """Attach to a tmux session.

    Args:
        session_name: Name of the session to attach to
    """
    from ..adapters.terminal._tmux import attach_session

    attach_session(session_name)


# Need to import the type for the return annotation
from ..adapters.terminal._tmux import TmuxManager


# =============================================================================
# Terminal Providers (iTerm2)
# =============================================================================


def is_running_in_iterm2() -> bool:
    """Check if currently running inside iTerm2.

    Returns:
        True if running in iTerm2
    """
    from ..adapters.terminal._iterm2 import is_running_in_iterm2 as _is_iterm

    return _is_iterm()


def discover_iterm_issue_tabs() -> list[int]:
    """Discover iTerm2 tabs that look like issue sessions.

    Returns:
        List of issue numbers found in tab names
    """
    from ..adapters.terminal._iterm2 import discover_issue_tabs

    return discover_issue_tabs()


def get_iterm_manager() -> "ITermSessionManager":
    """Get the iTerm2 session manager.

    Returns:
        ITermSessionManager instance
    """
    from ..adapters.terminal._iterm2 import get_iterm_manager

    return get_iterm_manager()


# Import for type annotation
from ..adapters.terminal._iterm2 import ITermSessionManager


# =============================================================================
# Worktree Providers
# =============================================================================


def get_hooks_dir() -> Path:
    """Get the hooks directory path.

    Returns:
        Path to the hooks directory
    """
    from ..adapters.worktree._worktree import HOOKS_DIR

    return HOOKS_DIR
