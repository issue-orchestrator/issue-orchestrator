"""Route + auth tests for POST /api/review-exchange/respond.

Covers the delivery outcomes the orchestrator-owned mailbox can return
(accepted / no-open-slot / already-delivered), malformed-body and
missing-mailbox handling, and the agent-callback auth gate.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.control_api import (
    configure_api_token,
    control_app,
    get_configured_agent_callback_token,
    get_configured_api_token,
)
from issue_orchestrator.entrypoints.control_api_issue_support import (
    ControlApiIssueDependencies,
    install_control_api_issue_dependencies,
)
from issue_orchestrator.execution.review_exchange_turn_mailbox import (
    InMemoryTurnMailbox,
)

KEY = "/wt/.issue-orchestrator/review-response.json"
AGENT_TOKEN = "test-agent-token"
AGENT_HEADERS = {"Authorization": f"Bearer {AGENT_TOKEN}"}


def _install_orchestrator(mailbox) -> None:
    orchestrator = SimpleNamespace(
        deps=SimpleNamespace(services=SimpleNamespace(turn_mailbox=mailbox))
    )
    install_control_api_issue_dependencies(
        control_app,
        ControlApiIssueDependencies(
            get_orchestrator=lambda: orchestrator,
            with_state_lock=lambda fn: fn(),
        ),
    )


@pytest.fixture
def client_with_mailbox():
    """TestClient with agent-callback auth + a real mailbox-backed orchestrator."""
    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    configure_api_token("test-admin-token", agent_callback=AGENT_TOKEN)
    mailbox = InMemoryTurnMailbox()
    _install_orchestrator(mailbox)
    try:
        yield TestClient(control_app), mailbox
    finally:
        configure_api_token(prev_admin, agent_callback=prev_agent)


def _post(client, payload):
    return client.post(
        "/api/review-exchange/respond", json=payload, headers=AGENT_HEADERS
    )


class TestDeliveryOutcomes:
    def test_accepted_delivers_into_open_slot(self, client_with_mailbox) -> None:
        client, mailbox = client_with_mailbox
        mailbox.open(KEY, turn_id="reviewer-r1-a1")
        resp = _post(
            client,
            {"key": KEY, "payload": {"response_type": "ok", "response_text": "good"}},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"
        assert mailbox.try_take(KEY) == {
            "response_type": "ok",
            "response_text": "good",
        }

    def test_no_open_slot_is_reported(self, client_with_mailbox) -> None:
        client, _ = client_with_mailbox
        resp = _post(
            client, {"key": KEY, "payload": {"response_type": "ok", "response_text": "x"}}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_open_slot"

    def test_duplicate_delivery_is_rejected(self, client_with_mailbox) -> None:
        client, mailbox = client_with_mailbox
        mailbox.open(KEY, turn_id="t")
        body = {"key": KEY, "payload": {"response_type": "ok", "response_text": "x"}}
        assert _post(client, body).json()["status"] == "accepted"
        second = _post(client, body)
        assert second.status_code == 200
        assert second.json()["status"] == "already_delivered"


class TestMalformed:
    def test_missing_key_is_400(self, client_with_mailbox) -> None:
        client, _ = client_with_mailbox
        resp = _post(client, {"payload": {"response_type": "ok", "response_text": "x"}})
        assert resp.status_code == 400
        assert "key is required" in resp.json()["detail"]

    def test_non_object_payload_is_400(self, client_with_mailbox) -> None:
        client, _ = client_with_mailbox
        resp = _post(client, {"key": KEY, "payload": "not-an-object"})
        assert resp.status_code == 400
        assert "JSON object" in resp.json()["detail"]

    def test_missing_payload_is_400(self, client_with_mailbox) -> None:
        client, _ = client_with_mailbox
        resp = _post(client, {"key": KEY})
        assert resp.status_code == 400


class TestMissingDependencies:
    def test_missing_mailbox_is_503(self, client_with_mailbox) -> None:
        client, _ = client_with_mailbox
        _install_orchestrator(None)  # orchestrator present, mailbox not configured
        resp = _post(
            client, {"key": KEY, "payload": {"response_type": "ok", "response_text": "x"}}
        )
        assert resp.status_code == 503
        assert "mailbox" in resp.json()["detail"].lower()

    def test_missing_orchestrator_is_503(self, client_with_mailbox) -> None:
        client, _ = client_with_mailbox
        install_control_api_issue_dependencies(
            control_app,
            ControlApiIssueDependencies(
                get_orchestrator=lambda: None,
                with_state_lock=lambda fn: fn(),
            ),
        )
        resp = _post(
            client, {"key": KEY, "payload": {"response_type": "ok", "response_text": "x"}}
        )
        assert resp.status_code == 503


class TestAuth:
    def test_missing_token_is_rejected(self, client_with_mailbox) -> None:
        client, mailbox = client_with_mailbox
        mailbox.open(KEY, turn_id="t")
        resp = client.post(
            "/api/review-exchange/respond",
            json={"key": KEY, "payload": {"response_type": "ok", "response_text": "x"}},
        )
        assert resp.status_code == 401

    def test_wrong_token_is_rejected(self, client_with_mailbox) -> None:
        client, _ = client_with_mailbox
        resp = client.post(
            "/api/review-exchange/respond",
            json={"key": KEY, "payload": {"response_type": "ok", "response_text": "x"}},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401
