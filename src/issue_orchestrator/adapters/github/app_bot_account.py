"""The account GitHub attributes a GitHub App installation's writes to (#8987).

An installation token acts as the App's bot user: ``<slug>[bot]``, an account
of type ``Bot`` with its own numeric id. GitHub's issue events name THAT
account as the actor of a label the App wrote, and leave
``performed_via_github_app`` null — so an event is the engine's own write when
its actor is the engine App's bot account, matched by GitHub's immutable
account id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import GitHubAuthError


@dataclass(frozen=True, slots=True)
class GitHubAppBotAccount:
    """The App's bot user: its login and GitHub's numeric account id."""

    login: str
    user_id: int

    def __post_init__(self) -> None:
        if not self.login.casefold().endswith("[bot]"):
            raise GitHubAuthError(f"{self.login!r} is not a GitHub App bot login")
        if type(self.user_id) is not int or self.user_id <= 0:
            raise GitHubAuthError(f"GitHub App bot {self.login!r} needs a positive account id")

    @classmethod
    def from_user_payload(cls, login: str, payload: Any) -> GitHubAppBotAccount:
        """Read ``GET /users/<login>``: it must be that login, a ``Bot`` account."""
        if not isinstance(payload, dict):
            raise GitHubAuthError(f"GitHub user payload for {login!r} was not an object")
        if str(payload.get("login") or "").casefold() != login.casefold():
            raise GitHubAuthError(
                f"GitHub answered {payload.get('login')!r} for the App bot {login!r}"
            )
        if payload.get("type") != "Bot":
            raise GitHubAuthError(
                f"GitHub App bot {login!r} is a {payload.get('type')!r} account, not a Bot"
            )
        user_id = payload.get("id")
        if type(user_id) is not int:
            raise GitHubAuthError(f"GitHub App bot {login!r} carries no numeric account id")
        return cls(login=login, user_id=user_id)


__all__ = ["GitHubAppBotAccount"]
