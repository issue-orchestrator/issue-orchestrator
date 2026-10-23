"""The Control Center's cross-repo Tech lead page (#7763).

Aggregation over configured repositories, engine-not-running rows, fail-closed
validation of each engine's answer, a running engine's last report when it
misses a read (#8988) — including page reads whose supervisor and engine
answers overlap — and the command proxy, through the owner and through the CC
routes.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from issue_orchestrator.contracts.ui_openapi_models import (
    ControlCenterTechLeadPayload,
    TechLeadProposalCommandPayload,
)
from issue_orchestrator.entrypoints.control_api import control_app
from issue_orchestrator.entrypoints.control_api_repo_support import (
    ControlApiRepoDependencies,
    get_control_api_repo_dependencies,
)
from issue_orchestrator.execution.engine_command_failure import EngineCommandFailure, EngineCommandFailureKind
from issue_orchestrator.execution.control_center_tech_lead import (
    ControlCenterTechLead,
    TechLeadSectionMemory,
    UnknownTechLeadRepositoryError,
)
from issue_orchestrator.infra.repo_identity import configured_repository_key
from issue_orchestrator.infra.repo_registry import RegisteredRepo
from issue_orchestrator.ports.repository_engine_supervisor import (
    MultiInstanceStatus,
    SupervisorStatus,
)
from tests.unit.threading_helpers import ThreadResult, join_or_fail, run_in_thread, wait_for_event


def _section(repository: str, waiting: int, generated_at: str = "now") -> dict:
    item = {
        "kind": "proposal", "number": 10, "operation": "reset_retry", "title": "t",
        "recommendation": "r", "approval_effect": "e", "link": "https://x", "waiting_since": "",
        "status": "awaiting_approval", "status_label": "Awaiting your approval",
        "can_approve": True, "can_decline": True, "details": [],
        "approval_steps": [], "operator_steps": [],
    }
    return {
        "repository": repository, "generated_at": generated_at, "waiting_count": waiting,
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
class _HeldCalls:
    """Holds the next call for a key in flight until released, as a slow
    supervisor or engine holds a page read: its answer is already decided."""

    gates: dict[object, tuple[threading.Event, threading.Event]] = field(default_factory=dict)

    def hold_next(self, key: object) -> tuple[threading.Event, threading.Event]:
        gate = self.gates[key] = (threading.Event(), threading.Event())
        return gate

    def pass_through(self, key: object) -> None:
        gate = self.gates.pop(key, None)
        if gate is not None:
            started, release = gate
            started.set()
            wait_for_event(release, 5, label="the held call's release")


@dataclass
class _Transport:
    sections: dict[int, dict | None] = field(default_factory=dict)
    answer: tuple[int, dict] | EngineCommandFailure | None = None
    commands: list[tuple[int, dict]] = field(default_factory=list)
    #: Section reads held in flight, by port.
    held: _HeldCalls = field(default_factory=_HeldCalls)

    def read_section(self, port):
        answer = self.sections.get(port)  # what the engine answers as the read starts
        self.held.pass_through(port)
        return answer

    def send_command(self, port, body):
        self.commands.append((port, body))
        return self.answer


def _owner(
    tmp_path, ports: dict[str, int | SupervisorStatus | None], transport: _Transport,
    memory: TechLeadSectionMemory | None = None, status_calls: _HeldCalls | None = None,
) -> tuple[ControlCenterTechLead, dict[str, str]]:
    """*ports* is read on every page read, so a test can stop, start or
    restart an engine: a port alone, or the supervisor's full status.
    *status_calls* holds a supervisor answer, by repository name."""
    held_status = status_calls or _HeldCalls()
    repos, keys = [], {}
    for name in ports:
        path = tmp_path / name
        path.mkdir(exist_ok=True)
        repos.append(RegisteredRepo(path=str(path), name=name))
        keys[name] = configured_repository_key(str(path))
    supervisor = MagicMock()

    def all_instances(path, *_args, **_kwargs):
        engine = ports[path.name]
        if not isinstance(engine, SupervisorStatus):
            engine = SupervisorStatus(state="running" if engine else "stopped", port=engine)
        held_status.pass_through(path.name)  # what it saw is decided; it lands later
        return MultiInstanceStatus(repo_root=str(path), instances=[engine])

    supervisor.status_all_instances.side_effect = all_instances
    return ControlCenterTechLead(supervisor, lambda: repos, transport, memory or TechLeadSectionMemory()), keys


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

    assert page.waiting_count is None  # unknown, never an all-clear (#8988)
    assert page.unreported_count == 2
    assert [row.availability for row in page.repos] == ["unavailable", "unavailable"]
    assert [row.detail for row in page.repos] == [
        "The engine's answer was not understood (version mismatch?).",
        "The engine is running but did not answer.",
    ]


def test_a_running_engine_that_misses_a_read_shows_its_last_report_and_no_count(tmp_path) -> None:
    """#8988: one missed read is what the engine last reported, as of when,
    and the page's total is unknown rather than the other engines' sum."""
    transport = _Transport(sections={8001: _section("o/a", 2, "2026-10-08T12:00:00+00:00"), 8002: _section("o/b", 1)})
    owner, _ = _owner(tmp_path, {"a": 8001, "b": 8002}, transport)
    assert owner.page().waiting_count == 3

    transport.sections[8001] = None  # a's engine is busy and misses this read
    page = owner.page()

    a = next(row for row in page.repos if row.name == "a")
    assert a.availability == "last_known"
    assert a.detail == "The engine is running but did not answer."
    assert a.section is not None and a.section.generated_at == "2026-10-08T12:00:00+00:00"
    assert [item.number for item in a.section.waiting] == [10, 11]
    assert page.waiting_count is None
    assert page.unreported_count == 1

    transport.sections[8001] = _section("o/a", 0, "2026-10-08T12:00:30+00:00")  # it answers again
    page = owner.page()
    assert [row.availability for row in page.repos] == ["available", "available"]
    assert page.waiting_count == 1 and page.unreported_count == 0


def test_an_off_contract_answer_also_falls_back_to_the_last_report(tmp_path) -> None:
    transport = _Transport(sections={8001: _section("o/a", 1)})
    owner, _ = _owner(tmp_path, {"a": 8001}, transport)
    owner.page()
    transport.sections[8001] = {"not": "the contract"}

    (row,) = owner.page().repos

    assert row.availability == "last_known"
    assert row.detail == "The engine's answer was not understood (version mismatch?)."
    assert row.section is not None and row.section.waiting_count == 1


def test_the_last_report_outlives_the_per_request_owner(tmp_path) -> None:
    """The Control Center builds the owner per request; the memory is its one."""
    memory = TechLeadSectionMemory()
    transport = _Transport(sections={8001: _section("o/a", 1)})
    first, _ = _owner(tmp_path, {"a": 8001}, transport, memory)
    first.page()
    transport.sections[8001] = None

    second, _ = _owner(tmp_path, {"a": 8001}, transport, memory)

    assert [row.availability for row in second.page().repos] == ["last_known"]


def test_a_stopped_engine_is_not_bridged_by_its_last_report(tmp_path) -> None:
    """Only a running engine's missed read shows its last report: once the
    engine stops, what it reported before is forgotten, so a restarted engine
    that has not answered yet is unknown, not its previous life's page."""
    ports: dict[str, int | SupervisorStatus | None] = {"a": 8001}
    transport = _Transport(sections={8001: _section("o/a", 1)})
    owner, _ = _owner(tmp_path, ports, transport)
    owner.page()

    ports["a"] = None
    stopped = owner.page()
    assert [(row.availability, row.section) for row in stopped.repos] == [("engine_not_running", None)]
    assert stopped.waiting_count == 0 and stopped.unreported_count == 1  # a stopped engine stays a lower bound

    ports["a"] = 8001
    transport.sections[8001] = None
    restarted = owner.page()
    assert [(row.availability, row.section) for row in restarted.repos] == [("unavailable", None)]
    assert restarted.waiting_count is None


def test_a_restarted_engine_never_shows_its_previous_lifes_report(tmp_path) -> None:
    """Even when no page read saw it stopped: a new process is a new life."""
    engines: dict[str, int | SupervisorStatus | None] = {
        "a": SupervisorStatus(state="running", port=8001, pid=100, started_at="2026-10-12T10:00:00+00:00"),
    }
    transport = _Transport(sections={8001: _section("o/a", 2)})
    owner, _ = _owner(tmp_path, engines, transport)
    owner.page()

    engines["a"] = SupervisorStatus(state="running", port=8001, pid=200, started_at="2026-10-12T10:05:00+00:00")
    transport.sections[8001] = None

    (row,) = owner.page().repos
    assert (row.availability, row.section) == ("unavailable", None)


@contextmanager
def _page_read_held(owner: ControlCenterTechLead, calls: _HeldCalls, key: object) -> Iterator[ThreadResult]:
    """One page read on its own thread, its call for *key* held in flight for
    the block; it lands on exit, and the result then holds the page it served."""
    started, release = calls.hold_next(key)
    thread, served = run_in_thread(owner.page)
    try:
        wait_for_event(started, 5, label="the page read reaching the held call")
        yield served
    finally:
        release.set()
        join_or_fail(thread, 5, label="the held page read")


@pytest.mark.parametrize("restart_read_first", [False, True], ids=["lands-first", "restart-reads-first"])
def test_a_read_landing_after_its_engine_stopped_is_not_remembered(tmp_path, restart_read_first) -> None:
    """r1 F1: a slow read of a running engine lands after another page read
    saw the engine stopped. It answers its own request, but what it read
    belongs to the stopped life: the restarted engine's missed read is
    unknown, never the previous life's page, whichever read comes first."""
    ports: dict[str, int | SupervisorStatus | None] = {"a": 8001}
    transport = _Transport(sections={8001: _section("o/a", 2, "2026-10-12T10:00:00+00:00")})
    owner, _ = _owner(tmp_path, ports, transport)

    with _page_read_held(owner, transport.held, 8001) as slow:
        ports["a"] = None
        assert [row.availability for row in owner.page().repos] == ["engine_not_running"]
        ports["a"] = 8001
        transport.sections[8001] = None  # restarted, and busy
        if restart_read_first:
            assert [(row.availability, row.section) for row in owner.page().repos] == [("unavailable", None)]

    assert [row.availability for row in slow.unwrap().repos] == ["available"]
    (row,) = owner.page().repos
    assert (row.availability, row.section) == ("unavailable", None)


def test_an_older_read_landing_last_does_not_replace_a_newer_report(tmp_path) -> None:
    """r1 F1: two page reads overlap and land out of order. The newer read's
    report is what a later missed read shows, not the older one's."""
    transport = _Transport(sections={8001: _section("o/a", 2, "2026-10-12T10:00:00+00:00")})
    owner, _ = _owner(tmp_path, {"a": 8001}, transport)

    with _page_read_held(owner, transport.held, 8001) as older:
        transport.sections[8001] = _section("o/a", 1, "2026-10-12T10:00:10+00:00")
        (newer,) = owner.page().repos
        assert newer.section is not None and newer.section.generated_at == "2026-10-12T10:00:10+00:00"

    (served,) = older.unwrap().repos
    assert served.section is not None and served.section.generated_at == "2026-10-12T10:00:00+00:00"
    transport.sections[8001] = None
    page = owner.page()
    (row,) = page.repos
    assert row.availability == "last_known"
    assert row.section is not None and row.section.generated_at == "2026-10-12T10:00:10+00:00"
    assert row.section.waiting_count == 1


def test_a_stale_running_observation_does_not_outlive_a_newer_stop(tmp_path) -> None:
    """r2 F1: page read A asks the supervisor first and sees the engine
    running, but its answer lands after read B saw the engine stopped. A
    still serves what it read, but the stop is newer: when the engine runs
    again and misses a read, A's answer is not its last report."""
    ports: dict[str, int | SupervisorStatus | None] = {"a": 8001}
    status_calls = _HeldCalls()
    transport = _Transport(sections={8001: _section("o/a", 2)})
    owner, _ = _owner(tmp_path, ports, transport, status_calls=status_calls)

    with _page_read_held(owner, status_calls, "a") as stale:
        ports["a"] = None
        assert [row.availability for row in owner.page().repos] == ["engine_not_running"]

    assert [row.availability for row in stale.unwrap().repos] == ["available"]
    ports["a"] = 8001
    transport.sections[8001] = None
    (row,) = owner.page().repos
    assert (row.availability, row.section) == ("unavailable", None)


def test_a_stale_stopped_observation_does_not_clear_a_newer_life(tmp_path) -> None:
    """r2 F1: page read A asks first and sees no engine, but its answer lands
    after read B saw the engine started and read its report. The newer life
    stays: its next missed read shows what B read."""
    ports: dict[str, int | SupervisorStatus | None] = {"a": None}
    status_calls = _HeldCalls()
    transport = _Transport(sections={8001: _section("o/a", 2, "2026-10-12T10:00:00+00:00")})
    owner, _ = _owner(tmp_path, ports, transport, status_calls=status_calls)

    with _page_read_held(owner, status_calls, "a") as stale:
        ports["a"] = 8001
        assert [row.availability for row in owner.page().repos] == ["available"]

    assert [row.availability for row in stale.unwrap().repos] == ["engine_not_running"]
    transport.sections[8001] = None
    (row,) = owner.page().repos
    assert row.availability == "last_known"
    assert row.section is not None and row.section.generated_at == "2026-10-12T10:00:00+00:00"


def test_a_stale_observation_of_an_earlier_life_does_not_replace_a_newer_one(tmp_path) -> None:
    """r2 F1: page read A sees the engine's earlier life, but its answer
    lands after read B saw the restarted life and read its report. The newer
    life stays current, report and all."""
    before = SupervisorStatus(state="running", port=8001, pid=100, started_at="2026-10-12T10:00:00+00:00")
    after = SupervisorStatus(state="running", port=8001, pid=200, started_at="2026-10-12T10:05:00+00:00")
    ports: dict[str, int | SupervisorStatus | None] = {"a": before}
    status_calls = _HeldCalls()
    transport = _Transport(sections={8001: _section("o/a", 2, "2026-10-12T10:05:10+00:00")})
    owner, _ = _owner(tmp_path, ports, transport, status_calls=status_calls)

    with _page_read_held(owner, status_calls, "a") as stale:
        ports["a"] = after
        assert [row.availability for row in owner.page().repos] == ["available"]

    assert [row.availability for row in stale.unwrap().repos] == ["available"]
    transport.sections[8001] = None
    (row,) = owner.page().repos
    assert row.availability == "last_known"
    assert row.section is not None and row.section.generated_at == "2026-10-12T10:05:10+00:00"


_KEY = "repo-" + "a" * 64


@pytest.mark.parametrize(
    "row",
    [
        {"availability": "last_known", "detail": "The engine is running but did not answer.", "section": None},
        {"availability": "available", "detail": "", "section": None},
        {"availability": "unavailable", "detail": "The engine is running but did not answer.", "section": _section("o/a", 1)},
        {"availability": "engine_not_running", "detail": "Engine not running.", "section": _section("o/a", 1)},
        {"availability": "last_known", "detail": "", "section": _section("o/a", 1)},
        {"availability": "unavailable", "detail": "", "section": None},
    ],
    ids=["last-known-without-section", "available-without-section", "unavailable-with-section",
         "not-running-with-section", "last-known-without-reason", "unavailable-without-reason"],
)
def test_the_contract_refuses_a_row_whose_availability_and_section_disagree(row) -> None:
    """r1 F2: availability decides whether a row carries a section."""
    payload = {"waiting_count": None, "unreported_count": 1, "generated_at": "now",
               "repos": [{"repo_key": _KEY, "name": "a", **row}]}
    with pytest.raises(ValidationError):
        ControlCenterTechLeadPayload.model_validate(payload)


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

    transport.sections[8001] = None  # the engine misses the next read
    stale = client.get("/api/control-center/tech-lead").json()
    assert stale["waiting_count"] is None  # serialized as unknown, never 0
    assert [(repo["availability"], repo["section"]["waiting_count"]) for repo in stale["repos"]] == [("last_known", 1)]

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
