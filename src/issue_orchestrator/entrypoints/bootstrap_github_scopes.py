"""The startup check that the configured GitHub token carries the right scopes.

Moved out of ``bootstrap`` (the composition root, over its line budget) as the
self-contained policy it is: required scopes must be present, disallowed ones
absent, and a GitHub App installation token is not an OAuth token at all.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..infra.config import Config

logger = logging.getLogger(__name__)


class TokenScopeSource(Protocol):
    """The one GitHub adapter capability this check reads."""

    def get_token_scopes(self) -> Iterable[str]: ...


def check_github_token_scopes(config: "Config", github: TokenScopeSource) -> None:
    if getattr(github, "auth_kind", None) == "github_app":
        logger.info("Skipping OAuth scope check for GitHub App installation auth")
        return
    required = {scope.strip() for scope in (config.github_required_scopes or []) if scope.strip()}
    allowed = {scope.strip() for scope in (config.github_allowed_scopes or []) if scope.strip()}
    try:
        scopes = set(github.get_token_scopes())
    except Exception as exc:
        logger.warning("Failed to fetch GitHub token scopes: %s", exc)
        return

    if required and not required.issubset(scopes):
        missing = sorted(required - scopes)
        raise ValueError(f"GitHub token missing required scopes: {missing}")

    if allowed and not scopes.issubset(allowed):
        extra = sorted(scopes - allowed)
        raise ValueError(f"GitHub token has disallowed scopes: {extra}")

    if scopes:
        logger.info("GitHub token scopes: %s", ", ".join(sorted(scopes)))
    else:
        logger.info("GitHub token scopes unavailable (fine-grained token or missing header)")


__all__ = ["check_github_token_scopes"]
