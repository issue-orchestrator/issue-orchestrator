"""Helpers for repo-scoped GitHub configuration."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


class GitHubConfig(Protocol):
    """Config attributes needed by GitHub auth and event serialization."""

    github_token: str | None
    github_token_env: str | None
    github_keyring_service: str | None
    github_keyring_username: str | None
    github_app_client_id: str | None
    github_app_id: str | None
    github_app_installation_id: str | None
    github_app_private_key_path: str | None
    github_app_private_key_env: str | None
    github_api_url: str
    github_http_timeout_seconds: float
    github_cache_ttl_seconds: int
    github_required_scopes: list[str]
    github_allowed_scopes: list[str]


def apply_github_section(config: GitHubConfig, github_section: dict) -> None:
    """Load GitHub settings into config."""
    config.github_token = github_section.get("token")
    config.github_token_env = github_section.get("token_env")
    config.github_keyring_service = github_section.get("keyring_service")
    config.github_keyring_username = github_section.get("keyring_username")
    app_section = github_section.get("app", {}) or {}
    config.github_app_client_id = app_section.get("client_id")
    config.github_app_id = app_section.get("app_id")
    config.github_app_installation_id = app_section.get("installation_id")
    config.github_app_private_key_path = app_section.get("private_key_path")
    config.github_app_private_key_env = app_section.get("private_key_env")
    config.github_api_url = github_section.get("api_url", "https://api.github.com")
    config.github_http_timeout_seconds = github_section.get("http_timeout_seconds", 20.0)
    config.github_cache_ttl_seconds = github_section.get("cache_ttl_seconds", 300)
    config.github_required_scopes = _scope_list(github_section.get("required_scopes", []) or [])
    config.github_allowed_scopes = _scope_list(github_section.get("allowed_scopes", []) or [])


def github_auth_kwargs(config: GitHubConfig) -> dict[str, str | None]:
    """Return repo-scoped GitHub auth settings keyed for auth helpers."""
    return {
        "configured_token": config.github_token,
        "configured_env": config.github_token_env,
        "configured_keyring_service": config.github_keyring_service,
        "configured_keyring_username": config.github_keyring_username,
        "configured_app_client_id": config.github_app_client_id,
        "configured_app_id": config.github_app_id,
        "configured_app_installation_id": config.github_app_installation_id,
        "configured_app_private_key_path": config.github_app_private_key_path,
        "configured_app_private_key_env": config.github_app_private_key_env,
    }


def github_app_auth_configured(config: GitHubConfig) -> bool:
    """Return whether any GitHub App auth field is configured."""
    return any(
        (
            config.github_app_client_id,
            config.github_app_id,
            config.github_app_installation_id,
            config.github_app_private_key_path,
            config.github_app_private_key_env,
        )
    )


def github_auth_event_fields(config: GitHubConfig) -> dict[str, object]:
    """Return non-secret GitHub auth/source config for event payloads."""
    return {
        "token_env": config.github_token_env,
        "keyring_service": config.github_keyring_service,
        "keyring_username": config.github_keyring_username,
        "app": {
            "client_id": config.github_app_client_id,
            "app_id": config.github_app_id,
            "installation_id": config.github_app_installation_id,
            "private_key_path": config.github_app_private_key_path,
            "private_key_env": config.github_app_private_key_env,
        },
        "api_url": config.github_api_url,
        "http_timeout_seconds": config.github_http_timeout_seconds,
        "cache_ttl_seconds": config.github_cache_ttl_seconds,
        "required_scopes": list(config.github_required_scopes),
        "allowed_scopes": list(config.github_allowed_scopes),
    }


def _scope_list(raw: object) -> list[str]:
    if isinstance(raw, str):
        return [scope.strip() for scope in raw.split(",") if scope.strip()]
    if isinstance(raw, Iterable):
        return [str(scope) for scope in raw]
    raise TypeError("GitHub scopes must be a string or iterable")
