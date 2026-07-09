"""Milestone checks for doctor."""

from __future__ import annotations

from typing import Any

from ..types import Check
from ...config import Config
from ....adapters.github.errors import GitHubAuthError
from ....adapters.github.auth import build_github_auth
from ....adapters.github.http_client import GitHubHttpClient, GitHubHttpConfig
from ....adapters.github.repo import get_repo_from_git, GitRepoError


def _resolve_repo(config: Config) -> str | None:
    if config.repo:
        return config.repo
    try:
        return get_repo_from_git()
    except GitRepoError:
        return None


def _extract_milestone_titles(payload: Any) -> set[str]:
    if not isinstance(payload, list):
        return set()
    titles: set[str] = set()
    for item in payload:
        if isinstance(item, dict):
            title = item.get("title")
            if isinstance(title, str):
                titles.add(title)
    return titles


def check_milestone_order(config: Config) -> list[Check]:
    """Verify milestones.order entries exist in the repo (open milestones)."""
    if not config.milestone_order:
        return []

    repo = _resolve_repo(config)
    if not repo:
        return [Check(
            name="Milestone Order",
            status="error",
            detail="Cannot validate milestones.order without a repository",
        )]

    try:
        auth = build_github_auth(
            **config.github_auth_kwargs(),
            repo=repo,
            api_url=config.github_api_url,
            timeout_seconds=float(config.github_http_timeout_seconds),
        )
    except GitHubAuthError as exc:
        return [Check(
            name="Milestone Order",
            status="error",
            detail=str(exc),
        )]

    client = GitHubHttpClient(GitHubHttpConfig(
        repo=repo,
        base_url=config.github_api_url,
        timeout_seconds=config.github_http_timeout_seconds,
        auth=auth,
    ))
    try:
        milestones = client.list_milestones(state="open")
    except Exception as exc:
        return [Check(
            name="Milestone Order",
            status="error",
            detail=f"Failed to list milestones: {exc}",
        )]
    finally:
        client.close()

    titles = _extract_milestone_titles(milestones)
    missing = [name for name in config.milestone_order if name not in titles]
    if missing:
        missing_display = ", ".join(missing)
        return [Check(
            name="Milestone Order",
            status="error",
            detail=f"Missing milestones: {missing_display}",
        )]

    return [Check(
        name="Milestone Order",
        status="ok",
        detail="All ordered milestones found",
    )]
