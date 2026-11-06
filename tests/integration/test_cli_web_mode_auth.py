"""One admin bearer works on both surfaces of a CLI-started web engine (#7315).

``issue-orchestrator start`` serves a dashboard and a Control API. The Control
API configured its tokens for itself, and nothing on this launch path configured
the dashboard's, whose gate was armed by the shared agent-callback token. So the
same bearer that passed one port was refused as ``invalid`` on the other, and
the Control Center, which probes the dashboard port, read a healthy engine as
one that "did not answer".

This drives the real launch path with a real Control API on an auto-assigned
port, and makes both requests from inside the running mode.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from issue_orchestrator.entrypoints import cli_run_modes, web
from issue_orchestrator.entrypoints.control_api import get_orchestrator
from issue_orchestrator.infra.agent_callback_endpoint import (
    RuntimeAgentCallbackEndpoint,
)
from issue_orchestrator.infra.api_token import AGENT_CALLBACK_TOKEN_ENV_VAR

ADMIN_TOKEN = "cli-web-mode-admin-token-long-enough"
BEARER = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def orchestrator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    # ``ControlAPIServer.start`` replaces the module's orchestrator; recording the
    # current one here puts it back at teardown.
    monkeypatch.setattr(
        "issue_orchestrator.entrypoints.control_api._orchestrator", get_orchestrator()
    )
    orch = MagicMock()
    orch.config.repo_root = tmp_path
    # ``None`` keeps browser_session on its defaults; a MagicMock would coerce to 1.
    orch.config.browser_session_ttl_seconds = None
    orch.config.sse_token_ttl_seconds = None
    orch.config.browser_session_max = None
    orch.deps.agent_callback_endpoint = RuntimeAgentCallbackEndpoint()
    return orch


# Each surface is asserted on its auth verdict only: past the gate, a stub
# orchestrator may answer 500 or 503, and either still means the bearer passed.
@pytest.mark.integration
def test_one_admin_bearer_is_accepted_on_both_surfaces(
    orchestrator: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # From the environment, so the test neither reads nor creates a real token file.
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv(AGENT_CALLBACK_TOKEN_ENV_VAR, "cli-web-mode-agent-token")
    monkeypatch.setattr(cli_run_modes, "console", MagicMock(), raising=False)
    statuses: dict[str, int] = {}

    async def serve(*_args: object, **_kwargs: object) -> None:
        api_port = orchestrator.deps.agent_callback_endpoint.resolve_port(0)
        async with httpx.AsyncClient() as client:
            answer = await client.get(f"http://127.0.0.1:{api_port}/api/status", headers=BEARER)
            statuses["control_api"] = answer.status_code
        transport = httpx.ASGITransport(app=web.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://dashboard") as client:
            statuses["dashboard"] = (await client.get("/api/status", headers=BEARER)).status_code

    monkeypatch.setattr(web, "run_with_web_dashboard", serve)

    asyncio.run(
        asyncio.wait_for(
            cli_run_modes.run_web_dashboard_mode(
                orchestrator, orchestrator.config, MagicMock(port=8080), 0
            ),
            timeout=60,
        )
    )

    assert statuses.keys() == {"control_api", "dashboard"}
    assert statuses["control_api"] != 401, statuses
    assert statuses["dashboard"] != 401, statuses
