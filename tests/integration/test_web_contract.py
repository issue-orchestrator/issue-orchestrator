"""Contract tests ensuring orchestrator and web layer stay in sync.

These tests verify that both the real Orchestrator and MockOrchestratorForWeb
satisfy the OrchestratorForWeb protocol, catching drift between them.

The structural protocol pins signatures; the route tests below also pin the
shared mock's generated response payloads.
"""

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.contracts.ui_openapi_models import (
    SessionFailureDiagnosisPayload,
)
from issue_orchestrator.entrypoints import web as web_module
from issue_orchestrator.ports.web_contract import OrchestratorForWeb
from tests.fixtures.web_contract_mocks import MockOrchestratorForWeb


class TestOrchestratorWebContract:
    """Verify orchestrator implementations satisfy the web contract."""

    def test_real_orchestrator_satisfies_protocol(self, sample_orchestrator):
        """Real Orchestrator must satisfy OrchestratorForWeb protocol."""
        assert isinstance(sample_orchestrator, OrchestratorForWeb), (
            "Orchestrator no longer satisfies OrchestratorForWeb protocol. "
            "Update the protocol or fix the Orchestrator."
        )

    def test_mock_orchestrator_satisfies_protocol(self):
        """MockOrchestratorForWeb must satisfy OrchestratorForWeb protocol."""
        mock = MockOrchestratorForWeb()
        assert isinstance(mock, OrchestratorForWeb), (
            "MockOrchestratorForWeb no longer satisfies OrchestratorForWeb protocol. "
            "Update the mock to match the real Orchestrator."
        )

    def test_protocol_attributes_exist_on_real(self, sample_orchestrator):
        """Verify required attributes exist on real orchestrator."""
        assert hasattr(sample_orchestrator, "state")
        assert hasattr(sample_orchestrator, "config")
        assert hasattr(sample_orchestrator, "shutdown_requested")  # Public property
        assert callable(getattr(sample_orchestrator, "pause", None))
        assert callable(getattr(sample_orchestrator, "resume", None))
        assert callable(getattr(sample_orchestrator, "request_shutdown", None))
        assert callable(getattr(sample_orchestrator, "request_refresh", None))
        # Read-model facades the dashboard route resolves before rendering.
        assert callable(getattr(sample_orchestrator.provider_circuit, "snapshot", None))
        assert callable(
            getattr(sample_orchestrator.tech_lead_run_history, "recent", None)
        )

    def test_protocol_attributes_exist_on_mock(self):
        """Verify required attributes exist on mock orchestrator."""
        mock = MockOrchestratorForWeb()
        assert hasattr(mock, "state")
        assert hasattr(mock, "config")
        assert hasattr(mock, "shutdown_requested")  # Public property
        assert callable(getattr(mock, "pause", None))
        assert callable(getattr(mock, "resume", None))
        assert callable(getattr(mock, "request_shutdown", None))
        assert callable(getattr(mock, "request_refresh", None))
        # A double without these makes every dashboard page render raise, so the
        # gap belongs here rather than in a browser suite (#6858 rework 1).
        assert callable(getattr(mock.provider_circuit, "snapshot", None))
        assert callable(getattr(mock.tech_lead_run_history, "recent", None))


@pytest.fixture
def dashboard_client():
    """The dashboard app with the shared web double installed.

    Restores whatever orchestrator was there, so this cannot leak into the
    module-scoped browser fixtures that share ``set_orchestrator``.
    """
    original = web_module.get_orchestrator()
    web_module.configure_dashboard_admin_token(None)
    web_module.set_orchestrator(MockOrchestratorForWeb())
    try:
        yield TestClient(web_module.app)
    finally:
        web_module.set_orchestrator(original)


def test_dashboard_page_renders_against_the_shared_double(dashboard_client):
    """``GET /`` must RENDER for the double, not just type-check against it.

    The protocol above says which members exist; this says the page actually
    builds from them. It is the cheap half of a guarantee that used to be held
    only by the Playwright suite: when the route started resolving a read-model
    facade the double did not carry, every dashboard render raised, and the
    first thing to notice was a browser test several suites and ~12 CI-minutes
    later (#6858 rework 1). A 500 here costs a second.
    """
    response = dashboard_client.get("/")

    assert response.status_code == 200, response.text
    assert "text/html" in response.headers["content-type"]


def test_failure_diagnosis_payload_satisfies_the_ui_contract():
    diagnosis = MockOrchestratorForWeb().get_failure_diagnosis(4057)

    payload = SessionFailureDiagnosisPayload.model_validate(diagnosis)

    assert payload.issue_number == 4057
    assert payload.review_feedback == []
    assert payload.analysis_headline is None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/failure-diagnosis/4057"),
        ("post", "/api/issues/4057/audit"),
    ],
)
def test_contracted_diagnosis_routes_serve_the_shared_mock(
    dashboard_client: TestClient,
    method: str,
    path: str,
):
    response = getattr(dashboard_client, method)(path)

    assert response.status_code == 200, response.text
    body = response.json()
    SessionFailureDiagnosisPayload.model_validate(body)
    assert body["issue_number"] == 4057
