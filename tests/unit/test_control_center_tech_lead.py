"""The Control Center's cross-repo Tech lead page (#7763).

Aggregation over configured repositories, engine-not-running rows, fail-closed
validation of each engine's answer, and the command proxy — through the owner
and through the CC routes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.contracts.ui_openapi_models import TechLeadProposalCommandPayload
from issue_orchestrator.entrypoints.control_api import control_app
from issue_orchestrator.entrypoints.control_api_repo_support import (
    ControlApiRepoDependencies,
    get_control_api_repo_dependencies,
)
from issue_orchestrator.execution.engine_command_failure import EngineCommandFailure, EngineCommandFailureKind
from issue_orchestrator.execution.control_center_tech_lead import (
    ControlCenterTechLead,
    UnknownTechLeadRepositoryError,
)
from issue_orchestrator.infra.repo_identity import configured_repository_key
from issue_orchestrator.infra.repo_registry import RegisteredRepo
from issue_orchestrator.ports.repository_engine_supervisor import (
    MultiInstanceStatus,
    SupervisorStatus,
)


def _section(repository: str, waiting: int) -> dict:
    item = {
        "kind": "proposal", "number": 10, "operation": "reset_retry", "title": "t",
        "recommendation": "r", "approval_effect": "e", "link": "https://x", "waiting_since": "",
        "status": "awaiting_approval", "status_label": "Awaiting your approval",
        "can_approve": True, "can_decline": True, "details": [],
        "approval_steps": [], "operator_steps": [],
    }
    return {
        "repository": repository, "generated_at": "now", "waiting_count": waiting,
        "run": {"has_run": False, "label": "", "phase": "", "phase_label": "", "started_at": "", "ended_at": "", "detail": ""},
        "waiting": [dict(item, number=10 + index) for index in range(waiting)],
        "doing": [], "parked": [], "triaged": [], "case_files": [],
        "health_review": {"enabled": False, "interval_minutes": 0, "last_at": "", "next_due_at": "", "label": "off"},
    }


_NO_ANSWER = EngineCommandFailure(
    kind=EngineCommandFailureKind.NO_ANSWER,
    command="tech-lead proposal decision",
    url="http://127.0.0.1:8001/api/tech-lead/proposals",
    detail="Engine did not answer tech-lead proposal decision within 120s (ReadTimeout)",
)


@dataclass
class _Transport:
    sections: dict[int, dict | None] = field(default_factory=dict)
    answer: tuple[int, dict] | EngineCommandFailure | None = None
    commands: list[tuple[int, dict]] = field(default_factory=list)

    def read_section(self, port):
        return self.sections.get(port)

    def send_command(self, port, body):
        self.commands.append((port, body))
        return self.answer


def _owner(tmp_path, ports: dict[str, int | None], transport: _Transport) -> tuple[ControlCenterTechLead, dict[str, str]]:
    repos, keys = [], {}
    for name, port in ports.items():
        path = tmp_path / name
        path.mkdir()
        repos.append(RegisteredRepo(path=str(path), name=name))
        keys[name] = configured_repository_key(str(path))
    supervisor = MagicMock()

    def all_instances(path, *_args, **_kwargs):
        port = ports[path.name]
        status = SupervisorStatus(state="running" if port else "stopped", port=port)
        return MultiInstanceStatus(repo_root=str(path), instances=[status])

    supervisor.status_all_instances.side_effect = all_instances
    return ControlCenterTechLead(supervisor, lambda: repos, transport), keys


def test_page_sums_waiting_across_running_engines_and_names_stopped_ones(tmp_path) -> None:
    transport = _Transport(sections={8001: _section("o/a", 2), 8002: _section("o/b", 1)})
    owner, _ = _owner(tmp_path, {"a": 8001, "b": 8002, "c": None}, transport)

    page = owner.page()

    assert page.waiting_count == 3
    # #7763 review r3 F3: c's backlog is unknown, so the count is a lower bound.
    assert page.unreported_count == 1
    availability = {row.name: row.availability for row in page.repos}
    assert availability == {"a": "available", "b": "available", "c": "engine_not_running"}
    assert next(row for row in page.repos if row.name == "c").section is None


def test_an_engine_answering_off_contract_is_unavailable_not_trusted(tmp_path) -> None:
    bad = _section("o/a", 1)
    bad["waiting"][0]["status"] = "approved-by-magic"
    transport = _Transport(sections={8001: bad, 8002: None})
    owner, _ = _owner(tmp_path, {"a": 8001, "b": 8002}, transport)

    page = owner.page()

    assert page.waiting_count == 0
    assert page.unreported_count == 2  # zero known, never an all-clear
    assert [row.availability for row in page.repos] == ["unavailable", "unavailable"]


def test_command_reaches_only_the_owning_engine(tmp_path) -> None:
    transport = _Transport(answer=(200, {"proposal_issue_number": 10, "outcome": "approved", "detail": "ok"}))
    owner, keys = _owner(tmp_path, {"a": 8001, "b": 8002}, transport)

    result = owner.command(keys["b"], TechLeadProposalCommandPayload(proposal_issue_number=10, decision="approve"))

    assert result.status_code == 200 and result.outcome.outcome == "approved"
    assert transport.commands == [(8002, {"proposal_issue_number": 10, "decision": "approve"})]


@pytest.mark.parametrize(
    ("port", "answer", "status"),
    [
        (None, None, 503),  # engine not running: approve on GitHub instead
        (8001, _NO_ANSWER, 503),  # engine did not answer
        (8001, (409, {"proposal_issue_number": 10, "outcome": "unavailable", "detail": "closed"}), 409),
        (8001, (200, {"unexpected": True}), 503),  # off-contract answer
    ],
)
def test_command_refusals_are_typed(tmp_path, port, answer, status) -> None:
    owner, keys = _owner(tmp_path, {"a": port}, _Transport(answer=answer))
    result = owner.command(keys["a"], TechLeadProposalCommandPayload(proposal_issue_number=10, decision="decline"))
    assert result.status_code == status and result.outcome.proposal_issue_number == 10


def test_a_failed_engine_command_names_its_cause(tmp_path) -> None:
    """#8222: the refusal carries the transport cause, never a bare 'did not answer'."""
    owner, keys = _owner(tmp_path, {"a": 8001}, _Transport(answer=_NO_ANSWER))

    result = owner.command(keys["a"], TechLeadProposalCommandPayload(proposal_issue_number=10, decision="approve"))

    assert result.status_code == 503
    assert result.outcome.outcome == "unavailable"
    assert _NO_ANSWER.detail in result.outcome.detail


def test_unknown_repository_key_raises(tmp_path) -> None:
    owner, _ = _owner(tmp_path, {"a": 8001}, _Transport())
    with pytest.raises(UnknownTechLeadRepositoryError):
        owner.command("repo-" + "0" * 64, TechLeadProposalCommandPayload(proposal_issue_number=1, decision="approve"))


@pytest.fixture
def cc(monkeypatch, fake_browser_auth, tmp_path):
    transport = _Transport(sections={8001: _section("o/a", 1)},
                           answer=(200, {"proposal_issue_number": 10, "outcome": "declined", "detail": "closed"}))
    owner, keys = _owner(tmp_path, {"a": 8001}, transport)
    deps = ControlApiRepoDependencies(
        get_supervisor=MagicMock(), get_control_actions=MagicMock(), validate_repo_root=MagicMock(),
        get_preferred_repo_root=MagicMock(), get_expected_engine_identity_raw=MagicMock(),
        get_recovery_queries=MagicMock(), get_recovery_stops=MagicMock(), get_tech_lead=lambda: owner,
    )
    monkeypatch.setitem(control_app.dependency_overrides, get_control_api_repo_dependencies, lambda: deps)
    return TestClient(control_app, headers=fake_browser_auth.bearer_headers()), keys, transport


def test_cc_routes_serve_the_page_and_proxy_the_command(cc) -> None:
    client, keys, transport = cc
    page = client.get("/api/control-center/tech-lead")
    assert page.status_code == 200 and page.json()["waiting_count"] == 1

    command = client.post(f"/api/control-center/repositories/{keys['a']}/tech-lead/proposals",
                          json={"proposal_issue_number": 10, "decision": "decline"})
    assert command.status_code == 200 and command.json()["outcome"] == "declined"
    assert transport.commands == [(8001, {"proposal_issue_number": 10, "decision": "decline"})]


def test_cc_command_route_rejects_unknown_repo_and_bad_bodies(cc) -> None:
    client, keys, _ = cc
    missing = client.post("/api/control-center/repositories/repo-" + "0" * 64 + "/tech-lead/proposals",
                          json={"proposal_issue_number": 10, "decision": "approve"})
    assert missing.status_code == 404 and missing.json()["outcome"] == "unavailable"
    bad = client.post(f"/api/control-center/repositories/{keys['a']}/tech-lead/proposals",
                      json={"proposal_issue_number": 10, "decision": "merge"})
    assert bad.status_code == 422
