"""Repository-scoped authentication for tech-lead finding promotion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class PromotionTargetGitHubAuthConfig:
    """Non-secret GitHub credential references for one promotion target."""

    token_env: Optional[str] = None
    keyring_service: Optional[str] = None
    keyring_username: Optional[str] = None
    app_client_id: Optional[str] = None
    app_id: Optional[str] = None
    app_installation_id: Optional[str] = None
    app_private_key_path: Optional[str] = None
    app_private_key_env: Optional[str] = None
    api_url: str = "https://api.github.com"
    http_timeout_seconds: float = 20.0

    @classmethod
    def from_mapping(cls, *, repo: str, data: Any) -> "PromotionTargetGitHubAuthConfig":
        if not isinstance(data, dict):
            raise ValueError(
                f"tech_lead.findings.target_auth[{repo!r}] must be a mapping"
            )
        unknown = sorted(
            set(data)
            - {
                "token_env",
                "keyring_service",
                "keyring_username",
                "app",
                "api_url",
                "http_timeout_seconds",
            }
        )
        if unknown:
            raise ValueError(
                f"tech_lead.findings.target_auth[{repo!r}] has unknown key(s)"
                f" {', '.join(unknown)}"
            )
        app = data.get("app", {}) or {}
        if not isinstance(app, dict):
            raise ValueError(
                f"tech_lead.findings.target_auth[{repo!r}].app must be a mapping"
            )
        unknown_app = sorted(
            set(app)
            - {
                "client_id",
                "app_id",
                "installation_id",
                "private_key_path",
                "private_key_env",
            }
        )
        if unknown_app:
            raise ValueError(
                f"tech_lead.findings.target_auth[{repo!r}].app has unknown key(s)"
                f" {', '.join(unknown_app)}"
            )
        return cls(
            token_env=_optional_text(data.get("token_env")),
            keyring_service=_optional_text(data.get("keyring_service")),
            keyring_username=_optional_text(data.get("keyring_username")),
            app_client_id=_optional_text(app.get("client_id")),
            app_id=_optional_text(app.get("app_id")),
            app_installation_id=_optional_text(app.get("installation_id")),
            app_private_key_path=_optional_text(app.get("private_key_path")),
            app_private_key_env=_optional_text(app.get("private_key_env")),
            api_url=str(data.get("api_url", "https://api.github.com")),
            http_timeout_seconds=float(data.get("http_timeout_seconds", 20.0)),
        )

    def startup_errors(self, *, repo: str) -> list[str]:
        prefix = f"tech_lead.findings.target_auth[{repo!r}]"
        personal = bool(self.token_env or self.keyring_service or self.keyring_username)
        app = bool(
            self.app_client_id
            or self.app_id
            or self.app_installation_id
            or self.app_private_key_path
            or self.app_private_key_env
        )
        errors: list[str] = []
        if personal == app:
            errors.append(
                f"{prefix} must configure exactly one personal-token source or app"
            )
        if personal and bool(self.keyring_service) != bool(self.keyring_username):
            errors.append(
                f"{prefix}.keyring_service and keyring_username must be set together"
            )
        if app:
            if not (self.app_client_id or self.app_id):
                errors.append(f"{prefix}.app requires client_id or app_id")
            if not self.app_installation_id:
                errors.append(f"{prefix}.app requires installation_id")
            if not (self.app_private_key_path or self.app_private_key_env):
                errors.append(
                    f"{prefix}.app requires private_key_path or private_key_env"
                )
        if self.http_timeout_seconds <= 0:
            errors.append(f"{prefix}.http_timeout_seconds must be > 0")
        return errors

    def auth_kwargs(self) -> dict[str, str | None]:
        return {
            "configured_env": self.token_env,
            "configured_keyring_service": self.keyring_service,
            "configured_keyring_username": self.keyring_username,
            "configured_app_client_id": self.app_client_id,
            "configured_app_id": self.app_id,
            "configured_app_installation_id": self.app_installation_id,
            "configured_app_private_key_path": self.app_private_key_path,
            "configured_app_private_key_env": self.app_private_key_env,
        }


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def parse_promotion_target_auth(
    raw: Any,
) -> dict[str, PromotionTargetGitHubAuthConfig]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            "tech_lead.findings.target_auth must be a mapping of target repo"
            f" -> GitHub auth, got {type(raw).__name__}"
        )
    result: dict[str, PromotionTargetGitHubAuthConfig] = {}
    folded_repos: set[str] = set()
    for raw_repo, raw_auth in raw.items():
        repo = str(raw_repo).strip()
        if not repo:
            raise ValueError(
                "tech_lead.findings.target_auth keys must be non-empty repositories"
            )
        folded_repo = repo.casefold()
        if folded_repo in folded_repos:
            raise ValueError(
                "tech_lead.findings.target_auth contains duplicate"
                f" case-insensitive repository {repo!r}"
            )
        folded_repos.add(folded_repo)
        result[repo] = PromotionTargetGitHubAuthConfig.from_mapping(
            repo=repo, data=raw_auth
        )
    return result


def promotion_target_auth_errors(
    target_auth: dict[str, PromotionTargetGitHubAuthConfig],
    routed_repos: tuple[str, ...],
) -> list[str]:
    routed = {repo.casefold() for repo in routed_repos}
    errors: list[str] = []
    for repo, auth in sorted(target_auth.items()):
        if repo.casefold() not in routed:
            errors.append(
                f"tech_lead.findings.target_auth[{repo!r}] does not match any"
                " foreign route target"
            )
        errors.extend(auth.startup_errors(repo=repo))
    return errors
