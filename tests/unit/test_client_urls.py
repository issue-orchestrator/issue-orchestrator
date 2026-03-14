from __future__ import annotations

import pytest

from issue_orchestrator.infra.client_urls import (
    resolve_client_base_url,
    resolve_client_dashboard_url,
    with_client_query_params,
)


def test_resolve_client_base_url_uses_localhost_for_wildcard_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODESPACE_NAME", raising=False)
    monkeypatch.delenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", raising=False)

    assert resolve_client_base_url(19080, local_host="0.0.0.0") == "http://localhost:19080"


@pytest.mark.parametrize("local_host", ["", "::", "[::]"])
def test_resolve_client_base_url_normalizes_empty_and_ipv6_wildcards(
    monkeypatch: pytest.MonkeyPatch,
    local_host: str,
) -> None:
    monkeypatch.delenv("CODESPACE_NAME", raising=False)
    monkeypatch.delenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", raising=False)

    assert resolve_client_base_url(19080, local_host=local_host) == "http://localhost:19080"


def test_resolve_client_base_url_uses_codespaces_forwarded_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODESPACE_NAME", "octo-space")
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")

    assert resolve_client_base_url(55543) == "https://octo-space-55543.app.github.dev"
    assert resolve_client_dashboard_url(55543) == "https://octo-space-55543.app.github.dev/"


def test_resolve_client_base_url_requires_positive_port() -> None:
    with pytest.raises(ValueError, match="Port must be positive"):
        resolve_client_base_url(0)


def test_with_client_query_params_appends_repo_path() -> None:
    result = with_client_query_params("https://octo-space-55543.app.github.dev/", repo="/workspaces/repo")

    assert result == "https://octo-space-55543.app.github.dev/?repo=%2Fworkspaces%2Frepo"
