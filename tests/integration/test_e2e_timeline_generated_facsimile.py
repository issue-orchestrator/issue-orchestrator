"""Generated 4057-style E2E run integration coverage.

Captured fixtures still protect backwards compatibility with real historical
runs. This test exercises the same HTTP surfaces with a facsimile generated
through current persistence APIs, which keeps most integration coverage from
depending on raw SQLite snapshots that drift as schemas evolve.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from fastapi.testclient import TestClient

from issue_orchestrator.contracts.ui_openapi_models import (
    E2ERunDetailPayload,
    E2ERunTimelinePayload,
    IssueDetailPayload,
)
from issue_orchestrator.entrypoints.control_api import (
    set_orchestrator as set_control_orchestrator,
)
from issue_orchestrator.entrypoints.web import app, set_orchestrator
from issue_orchestrator.execution.timeline_reader import DefaultTimelineReader
from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
from tests.fixtures.e2e_facsimile import (
    ISSUE_4057_NODEID,
    MaterializedE2EFacsimile,
    materialize_e2e_4057_facsimile,
)
from tests.fixtures.web_contract_mocks import MockOrchestratorForWeb


class _NeverRetryPublish:
    def can_retry_publish(self, issue_number: int, state: object) -> bool:
        _ = issue_number, state
        return False


def _mock_orchestrator_for(facsimile: MaterializedE2EFacsimile) -> MockOrchestratorForWeb:
    orchestrator = MockOrchestratorForWeb()
    orchestrator.config.repo = "test/repo"
    orchestrator.config.repo_root = facsimile.repo_root
    timeline_store = SqliteTimelineStore(facsimile.base_timeline_path)
    setattr(
        orchestrator,
        "deps",
        SimpleNamespace(
            timeline_store=timeline_store,
            timeline_reader=DefaultTimelineReader(timeline_store),
            publish_recovery=_NeverRetryPublish(),
        ),
    )
    return orchestrator


def _install_orchestrator(orchestrator: MockOrchestratorForWeb | None) -> None:
    set_orchestrator(orchestrator)
    set_control_orchestrator(cast(Any, orchestrator))


def _get_payload(client: TestClient, path: str, **params: str) -> dict:
    response = client.get(path, params=params)
    assert response.status_code == 200, (
        f"GET {path} returned HTTP {response.status_code}: {response.text}"
    )
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _issue_affordance_numbers(event: dict) -> list[int]:
    return sorted(
        affordance["issue_number"]
        for affordance in event.get("issue_affordances") or []
    )


def test_generated_4057_facsimile_exercises_run_detail_timeline_and_clickthrough(
    tmp_path,
) -> None:
    facsimile = materialize_e2e_4057_facsimile(tmp_path)
    orchestrator = _mock_orchestrator_for(facsimile)

    assert facsimile.e2e_db_path.exists()
    assert facsimile.base_timeline_path.exists()
    assert facsimile.worktree_timeline_path.exists()

    _install_orchestrator(orchestrator)
    try:
        client = TestClient(app)
        run_detail = _get_payload(client, f"/api/e2e-run-detail/{facsimile.run_id}")
        E2ERunDetailPayload.model_validate(run_detail)
        assert run_detail["run"]["id"] == facsimile.run_id
        assert run_detail["reports"], "facsimile run should expose generated report artifacts"

        test_4057_events = [
            event
            for event in run_detail["events"]
            if event.get("event") == "e2e.test_started"
            and event.get("nodeid") == ISSUE_4057_NODEID
        ]
        assert len(test_4057_events) == 1
        assert _issue_affordance_numbers(test_4057_events[0]) == [4057, 4058]
        assert all(
            affordance["run_id"] == facsimile.run_id
            for affordance in test_4057_events[0]["issue_affordances"]
        )

        smoke_events = [
            event
            for event in run_detail["events"]
            if event.get("event") == "e2e.test_started"
            and event.get("nodeid")
            == "tests/e2e/test_dashboard_smoke.py::test_control_center_loads"
        ]
        assert len(smoke_events) == 1
        assert smoke_events[0].get("issue_affordances") == []

        control_timeline = _get_payload(
            client,
            f"/control/e2e/run/{facsimile.run_id}/timeline",
            repo_root=str(facsimile.repo_root),
            view="user",
        )
        E2ERunTimelinePayload.model_validate(control_timeline)
        assert sorted(
            affordance["issue_number"]
            for affordance in control_timeline["issue_affordances"]
        ) == [4057, 4058]
        assert control_timeline["phase_toc"]
        lifecycle_runs = control_timeline["lifecycle"]["runs"]
        assert lifecycle_runs
        linked_lifecycles = lifecycle_runs[0]["e2e_run"]["linked_issue_lifecycles"]
        assert sorted(item["issue_number"] for item in linked_lifecycles) == [
            4057,
            4058,
        ]

        for issue_number in (4057, 4058):
            issue_detail = _get_payload(
                client,
                f"/api/e2e-run/{facsimile.run_id}/issue-detail/{issue_number}",
                view="user",
            )
            IssueDetailPayload.model_validate(issue_detail)
            assert issue_detail["e2e_run_id"] == facsimile.run_id
            assert issue_detail["events"]
            assert issue_detail["lifecycle"]["current"]["issue_lifecycles"][0][
                "cycles"
            ]
            action_types = {
                action["type"]
                for event in issue_detail["events"]
                for action in event.get("actions") or []
            }
            assert "open_agent_log" in action_types
            assert "open_review_transcript" in action_types
            assert "open_session_diagnostics" in action_types
            assert not [
                event
                for event in issue_detail["events"]
                if event.get("actions_error")
            ]
    finally:
        _install_orchestrator(None)
