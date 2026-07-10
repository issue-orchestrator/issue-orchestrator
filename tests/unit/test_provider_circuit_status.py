"""Provider circuit-breaker status: owner snapshot + UI projection (issue #5980).

Covers both sides of the command surface:
- producer/base system: ``ProviderResilienceManager.snapshot`` interprets the
  persisted circuit store into typed statuses (is-open / cooldown remaining).
- payload: ``build_provider_circuit_status`` projects those into the banner +
  health-panel view model, and the whole thing rides the public
  ``DashboardDataContract`` through the real dashboard view-model builder.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from issue_orchestrator.contracts.public import DashboardDataContract
from issue_orchestrator.control.provider_resilience import ProviderResilienceManager
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.infra.config import Config, ProviderResilienceConfig
from issue_orchestrator.ports.provider_resilience import (
    InMemoryProviderCircuitStore,
    ProviderCircuitState,
)
from issue_orchestrator.view_models.dashboard import build_dashboard_view_model
from issue_orchestrator.view_models.provider_circuit import (
    ProviderCircuitStatusView,
    build_provider_circuit_status,
)

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


class _NullEvents:
    def publish(self, *_args: object, **_kwargs: object) -> None:  # pragma: no cover - inert
        pass


def _manager(*states: ProviderCircuitState) -> ProviderResilienceManager:
    store = InMemoryProviderCircuitStore()
    for state in states:
        store.save(state)
    return ProviderResilienceManager(
        config=ProviderResilienceConfig(), store=store, events=_NullEvents()
    )


def _open(provider: str, seconds_remaining: int, *, outages: int = 1, error: str | None = None):
    return ProviderCircuitState(
        provider=provider,
        open_until=NOW + timedelta(seconds=seconds_remaining),
        consecutive_outages=outages,
        last_error_summary=error,
        updated_at=NOW,
    )


def _recovering(provider: str, *, outages: int = 1):
    # close_expired() leaves the row with open_until=None until a retry succeeds.
    return ProviderCircuitState(
        provider=provider,
        open_until=None,
        consecutive_outages=outages,
        last_error_summary="prior failure",
        updated_at=NOW,
    )


# --------------------------------------------------------------------------
# Owner snapshot (ProviderResilienceManager.snapshot)
# --------------------------------------------------------------------------

def test_snapshot_reports_open_circuit_with_cooldown_remaining():
    manager = _manager(_open("anthropic", 252, outages=3, error="HTTP 529"))

    (status,) = manager.snapshot(NOW)

    assert status.provider == "anthropic"
    assert status.is_open is True
    assert status.cooldown_remaining_seconds == 252
    assert status.consecutive_outages == 3
    assert status.last_error_summary == "HTTP 529"
    assert status.open_until == NOW + timedelta(seconds=252)


def test_snapshot_treats_elapsed_open_until_as_closed():
    # open_until already in the past -> not open, no cooldown, open_until dropped.
    manager = _manager(_open("openai", -5))

    (status,) = manager.snapshot(NOW)

    assert status.is_open is False
    assert status.cooldown_remaining_seconds == 0
    assert status.open_until is None


def test_snapshot_reports_recovering_circuit():
    manager = _manager(_recovering("gemini", outages=2))

    (status,) = manager.snapshot(NOW)

    assert status.is_open is False
    assert status.cooldown_remaining_seconds == 0
    assert status.consecutive_outages == 2


def test_snapshot_is_sorted_and_empty_when_no_outages():
    assert _manager().snapshot(NOW) == []

    manager = _manager(_open("openai", 60), _open("anthropic", 30))
    assert [s.provider for s in manager.snapshot(NOW)] == ["anthropic", "openai"]


# --------------------------------------------------------------------------
# UI projection (build_provider_circuit_status)
# --------------------------------------------------------------------------

def test_projection_single_open_provider_summary():
    manager = _manager(_open("anthropic", 252))

    view = build_provider_circuit_status(manager.snapshot(NOW))

    assert view.any_open is True
    assert view.open_count == 1
    assert view.open_providers == ("anthropic",)
    assert "anthropic unavailable" in view.summary_text
    assert "4m 12s" in view.summary_text
    (entry,) = view.entries
    assert entry.status_label == "Unavailable"
    assert entry.cooldown_remaining_label == "4m 12s"
    assert entry.next_retry_at == (NOW + timedelta(seconds=252)).isoformat()


def test_projection_multiple_open_providers_reports_soonest_retry():
    manager = _manager(_open("anthropic", 600), _open("openai", 90))

    view = build_provider_circuit_status(manager.snapshot(NOW))

    assert view.open_count == 2
    assert set(view.open_providers) == {"anthropic", "openai"}
    assert "2 providers unavailable" in view.summary_text
    # Banner advertises the *next* retry (the smallest remaining window).
    assert view.next_retry_at == (NOW + timedelta(seconds=90)).isoformat()
    assert "1m 30s" in view.summary_text


def test_projection_orders_open_before_recovering():
    manager = _manager(_recovering("gemini"), _open("openai", 120))

    view = build_provider_circuit_status(manager.snapshot(NOW))

    assert [e.provider for e in view.entries] == ["openai", "gemini"]
    assert view.entries[0].is_open is True
    assert view.entries[1].status_label == "Recovering"
    assert view.entries[1].cooldown_remaining_label is None


def test_projection_hides_banner_when_only_recovering():
    manager = _manager(_recovering("gemini"))

    view = build_provider_circuit_status(manager.snapshot(NOW))

    assert view.any_open is False
    assert view.summary_text == ""
    assert view.open_providers == ()
    # The recovering row is still available for the panel context.
    assert [e.provider for e in view.entries] == ["gemini"]


def test_projection_empty_status_is_hidden():
    view = ProviderCircuitStatusView.empty()
    assert view.any_open is False
    assert view.entries == ()


# --------------------------------------------------------------------------
# Integration through the dashboard view-model builder + public contract
# --------------------------------------------------------------------------

def _config() -> Config:
    config = Config()
    config.repo = "test/repo"
    config.repo_root = Path("/tmp/repo")
    config.e2e.enabled = False
    return config


def _orchestrator_with_manager(manager: ProviderResilienceManager | None):
    class _Stub:
        def __init__(self) -> None:
            self.state = OrchestratorState(startup_status="complete")
            self.config = _config()
            self.shutdown_requested = False
            if manager is not None:
                self.deps = type("_Deps", (), {"provider_resilience": manager})()

    return _Stub()


def test_dashboard_data_surfaces_open_circuit():
    manager = _manager(_open("anthropic", 300, error="overloaded"))
    orchestrator = _orchestrator_with_manager(manager)

    view_model = build_dashboard_view_model(
        orchestrator,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    circuit = view_model.dashboard_data()["providerCircuit"]
    assert circuit["any_open"] is True
    assert circuit["open_providers"] == ["anthropic"]
    # The whole dashboard_data payload validates against the public contract.
    DashboardDataContract.model_validate(view_model.dashboard_data())


def test_dashboard_data_circuit_empty_without_deps():
    # A pre-bootstrap orchestrator (no deps/manager) must not crash and must
    # still emit a well-formed, hidden circuit payload.
    orchestrator = _orchestrator_with_manager(None)

    view_model = build_dashboard_view_model(
        orchestrator,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    circuit = view_model.dashboard_data()["providerCircuit"]
    assert circuit["any_open"] is False
    assert circuit["entries"] == []
    DashboardDataContract.model_validate(view_model.dashboard_data())
