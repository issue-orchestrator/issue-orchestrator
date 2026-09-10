"""Composition tests for repository-scoped promotion credentials (#7232)."""

from unittest.mock import Mock, patch

import pytest

from issue_orchestrator.execution.providers import create_promotion_target_host
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models_tech_lead import (
    PromotionRouteTarget,
    PromotionTargetGitHubAuthConfig,
)


def _active_config() -> Config:
    config = Config()
    config.repo = "source/repo"
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_follow_up_agent = "agent:backend"
    return config


def test_target_repo_auth_is_built_once_and_given_to_the_adapter() -> None:
    repo = "issue-orchestrator/issue-orchestrator"
    config = _active_config()
    config.tech_lead.findings.route = {
        "completion-pipeline": PromotionRouteTarget(repo=repo),
        "session-runtime": PromotionRouteTarget(repo=repo),
    }
    config.tech_lead.findings.target_auth = {
        repo: PromotionTargetGitHubAuthConfig(
            app_client_id="client",
            app_installation_id="123",
            app_private_key_env="IO_APP_KEY",
        )
    }
    repository_host = Mock()
    auth = Mock()
    adapted = Mock()

    with (
        patch(
            "issue_orchestrator.adapters.github.build_github_auth",
            return_value=auth,
        ) as build_auth,
        patch(
            "issue_orchestrator.adapters.github.promotion_target."
            "build_promotion_target_host",
            return_value=adapted,
        ) as build_host,
        patch(
            "issue_orchestrator.adapters.github.promotion_target."
            "supports_promotion_target_host",
            return_value=True,
        ),
    ):
        result = create_promotion_target_host(repository_host, config)

    assert result is adapted
    build_auth.assert_called_once_with(
        configured_env=None,
        configured_keyring_service=None,
        configured_keyring_username=None,
        configured_app_client_id="client",
        configured_app_id=None,
        configured_app_installation_id="123",
        configured_app_private_key_path=None,
        configured_app_private_key_env="IO_APP_KEY",
        repo=repo,
        api_url="https://api.github.com",
        timeout_seconds=20.0,
    )
    build_host.assert_called_once()
    assert build_host.call_args.args == (repository_host,)
    connection = build_host.call_args.kwargs["target_connections"][repo]
    assert connection.repo == repo
    assert connection.base_url == "https://api.github.com"
    assert connection.timeout_seconds == 20.0
    assert connection.auth is auth


@pytest.mark.parametrize("inactive_mode", ("master_disabled", "promotion_off"))
def test_inactive_promotion_does_not_resolve_unavailable_target_auth(
    inactive_mode: str,
) -> None:
    repo = "issue-orchestrator/issue-orchestrator"
    config = _active_config()
    config.tech_lead.findings.route = {
        "completion-pipeline": PromotionRouteTarget(repo=repo),
    }
    config.tech_lead.findings.target_auth = {
        repo: PromotionTargetGitHubAuthConfig(token_env="MISSING_PROMOTION_TOKEN")
    }
    if inactive_mode == "master_disabled":
        config.tech_lead.enabled = False
    else:
        config.tech_lead.findings.promote = "off"

    with (
        patch(
            "issue_orchestrator.adapters.github.build_github_auth",
            side_effect=AssertionError("inactive lane resolved target auth"),
        ) as build_auth,
        patch(
            "issue_orchestrator.adapters.github.promotion_target."
            "build_promotion_target_host"
        ) as build_host,
    ):
        result = create_promotion_target_host(Mock(), config)

    assert result is None
    build_auth.assert_not_called()
    build_host.assert_not_called()


def test_active_unsupported_host_does_not_resolve_unavailable_target_auth() -> None:
    repo = "issue-orchestrator/issue-orchestrator"
    config = _active_config()
    config.tech_lead.findings.route = {
        "completion-pipeline": PromotionRouteTarget(repo=repo),
    }
    config.tech_lead.findings.target_auth = {
        repo: PromotionTargetGitHubAuthConfig(token_env="MISSING_PROMOTION_TOKEN")
    }

    with patch(
        "issue_orchestrator.adapters.github.build_github_auth",
        side_effect=AssertionError("unsupported host resolved target auth"),
    ) as build_auth:
        result = create_promotion_target_host(Mock(), config)

    assert result is None
    build_auth.assert_not_called()
