from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.mcp_server import McpApp, McpSettings, OrchestratorHttpClient
from issue_orchestrator.infra import supervisor


def _settings(*, host: str = "127.0.0.1") -> McpSettings:
    return McpSettings(
        repo_root=Path("/tmp/repo"),
        config_path=Path("/tmp/repo/.issue-orchestrator/config/default.yaml"),
        instance_id=None,
        host=host,
        auto_start=False,
    )


def test_http_client_keeps_internal_api_base_url_local() -> None:
    client = OrchestratorHttpClient(_settings(host="0.0.0.0"))
    client.update_port(55543)

    assert client.api_base_url() == "http://0.0.0.0:55543"


def test_http_client_resolves_client_base_url_for_codespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODESPACE_NAME", "octo-space")
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
    client = OrchestratorHttpClient(_settings())
    client.update_port(55543)

    assert client.client_base_url() == "https://octo-space-55543.app.github.dev"
    assert client.doctor_url() == "https://octo-space-55543.app.github.dev/api/doctor"


def test_mcp_urls_use_client_facing_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODESPACE_NAME", "octo-space")
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
    app = McpApp(_settings())
    app.override_port(55543)

    assert app.urls() == {
        "base_url": "https://octo-space-55543.app.github.dev",
        "dashboard_url": "https://octo-space-55543.app.github.dev/",
        "events_url": "https://octo-space-55543.app.github.dev/api/events",
        "config_url": "https://octo-space-55543.app.github.dev/api/config",
    }


def test_client_base_url_uses_supervisor_status_when_port_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OrchestratorHttpClient(_settings(host="0.0.0.0"))
    running = supervisor.SupervisorStatus(
        state="running",
        pid=123,
        port=19080,
        started_at=None,
        instance_id=None,
    )
    monkeypatch.setattr(
        "issue_orchestrator.entrypoints.mcp_server.supervisor.status",
        lambda repo_root, instance_id=None: running,
    )

    assert client.client_base_url() == "http://localhost:19080"
