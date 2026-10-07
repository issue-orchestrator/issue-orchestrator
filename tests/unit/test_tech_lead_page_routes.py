"""Engine side of the Tech lead page (#7763): both sides of the command boundary.

* producer -> payload: the engine's section route serves the page facade's
  projection, and the facade reads only engine-held state (no GitHub calls);
* command payload -> owner: the typed POST reaches the ONE approval owner and
  writes exactly the approval it records, and the retired rework-proposal
  routes are gone.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.standing_ruling_helpers import rulings_owner
from issue_orchestrator.adapters.github.github_issue import GitHubIssue
from issue_orchestrator.control.merge_hold_status import MergeHoldStatuses
from issue_orchestrator.domain.scoped_rework import TechLeadProposalCommand
from issue_orchestrator.domain.tech_lead_approval import APPROVED_LABEL, ApprovalVerdictKind
from issue_orchestrator.infra.tech_lead_proposal_facade import (
    proposal_command,
    tech_lead_page_section,
)
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from tests.approval_helpers import BOT, GATED, FakeApprovalEvidence, make_approvals

REPO = "porchpin/porchpin"


class _Host:
    """The GitHub writes an approval may make, recorded."""

    def __init__(self, evidence: FakeApprovalEvidence, issue: GitHubIssue) -> None:
        self.evidence, self.issue = evidence, issue
        self.writes: list[tuple] = []

    def get_issue(self, number):
        return self.issue if number == self.issue.number else None

    def add_label(self, number, label):
        self.writes.append(("add_label", number, label))
        self.evidence.engine_write(number, label)  # the engine's own App identity
        self.issue = GitHubIssue(number=number, repo=REPO, title=self.issue.title,
                                 labels=(*self.issue.labels, label), state=self.issue.state)

    def remove_label(self, number, label):
        self.writes.append(("remove_label", number, label))

    def add_comment(self, number, body):
        self.writes.append(("add_comment", number))
        return ""

    def update_issue_state(self, number, state):
        self.writes.append(("update_issue_state", number, state))
        self.issue = GitHubIssue(number=number, repo=REPO, title=self.issue.title,
                                 labels=self.issue.labels, state=state)


def _engine(issue: GitHubIssue, scope_label: str | None = None):
    evidence = FakeApprovalEvidence()
    approvals = make_approvals(evidence)
    host = _Host(evidence, issue)
    authority = InMemoryTechLeadAuthorityStore()
    orchestrator = SimpleNamespace(
        state_lock=threading.RLock(),
        config=SimpleNamespace(filtering=SimpleNamespace(label=scope_label)),
        state=SimpleNamespace(tech_lead_approval_scan_at=123.0),
        deps=SimpleNamespace(
            action_applier=SimpleNamespace(tech_lead_approvals=approvals),
            services=SimpleNamespace(tech_lead_authority=authority),
            repository_host=host,
        ),
    )
    return orchestrator, approvals, host


@pytest.fixture
def client(fake_browser_auth):
    from issue_orchestrator.entrypoints.web import app, set_orchestrator

    engine = MagicMock()
    set_orchestrator(engine)
    try:
        yield TestClient(app, headers=fake_browser_auth.bearer_headers()), engine
    finally:
        set_orchestrator(None)


def test_approve_route_records_an_operator_approval_the_owner_verifies(client) -> None:
    http, engine = client
    orchestrator, approvals, host = _engine(GitHubIssue(number=501, repo=REPO, title="p", labels=GATED))
    engine.request_tech_lead_proposal.side_effect = lambda command: proposal_command(orchestrator, command)

    response = http.post("/api/tech-lead/proposals", json={"proposal_issue_number": 501, "decision": "approve"})

    assert response.status_code == 200 and response.json()["outcome"] == "approved"
    assert ("add_label", 501, APPROVED_LABEL) in host.writes
    # The engine's (bot) label counts only because the operator's act is on record.
    assert approvals.verify(host.issue, fresh=True).kind is ApprovalVerdictKind.CONTROL_CENTER
    assert orchestrator.state.tech_lead_approval_scan_at == 0.0  # next tick acts on it


def test_decline_route_closes_the_proposal_and_executes_nothing(client) -> None:
    http, engine = client
    orchestrator, _, host = _engine(GitHubIssue(number=502, repo=REPO, title="p", labels=GATED))
    engine.request_tech_lead_proposal.side_effect = lambda command: proposal_command(orchestrator, command)

    response = http.post("/api/tech-lead/proposals", json={"proposal_issue_number": 502, "decision": "decline"})

    assert response.status_code == 200 and response.json()["outcome"] == "declined"
    assert ("update_issue_state", 502, "closed") in host.writes
    assert not any(write[0] == "add_label" for write in host.writes)


def test_refused_command_maps_to_409_with_the_typed_outcome(client) -> None:
    http, engine = client
    orchestrator, _, _ = _engine(GitHubIssue(number=503, repo=REPO, title="ordinary", labels=()))
    engine.request_tech_lead_proposal.side_effect = lambda command: proposal_command(orchestrator, command)

    response = http.post("/api/tech-lead/proposals", json={"proposal_issue_number": 503, "decision": "approve"})

    assert response.status_code == 409
    assert response.json() == {"proposal_issue_number": 503, "outcome": "unavailable",
                               "detail": "This issue is not a tech-lead proposal of this engine"}


def test_route_hands_the_owner_one_typed_command(client) -> None:
    http, engine = client
    engine.request_tech_lead_proposal.return_value = SimpleNamespace(
        outcome="approved", detail="ok", proposal_issue_number=7)
    http.post("/api/tech-lead/proposals", json={"proposal_issue_number": 7, "decision": "approve"})
    assert engine.request_tech_lead_proposal.call_args.args == (TechLeadProposalCommand(7, "approve"),)
    rejected = http.post("/api/tech-lead/proposals", json={"proposal_issue_number": 0, "decision": "approve"})
    assert rejected.status_code == 422


def test_section_route_serves_the_engine_projection(client) -> None:
    from issue_orchestrator.contracts.ui_openapi_models import (
        TechLeadHealthReviewPayload,
        TechLeadPageSectionPayload,
        TechLeadRunStripPayload,
    )

    http, engine = client
    engine.tech_lead_page_section.return_value = TechLeadPageSectionPayload(
        repository=REPO, generated_at="now", waiting_count=0,
        run=TechLeadRunStripPayload(has_run=False, label="", phase="", phase_label="", started_at="", ended_at="", detail=""),
        waiting=[], doing=[], parked=[], triaged=[], case_files=[],
        health_review=TechLeadHealthReviewPayload(enabled=False, interval_minutes=0, last_at="", next_due_at="", label="off"),
    )
    response = http.get("/api/tech-lead/page")
    assert response.status_code == 200 and response.json()["repository"] == REPO


def test_retired_rework_proposal_routes_are_gone(client) -> None:
    http, _ = client
    assert http.get("/api/tech-lead/rework-proposals").status_code == 404
    assert http.post("/api/tech-lead/rework-proposals",
                     json={"proposal_issue_number": 1, "decision": "approve"}).status_code in (404, 405)


class _WeakReferenceable:
    """An engine stand-in (attributes only)."""

    def __init__(self, **fields) -> None:
        self.__dict__.update(fields)


def _page_engine(approvals):
    from issue_orchestrator.control.label_manager import LabelManager
    from issue_orchestrator.infra.config import Config

    history = MagicMock()
    history.recent.return_value = ()
    claims = MagicMock()
    claims.list_needs_human_causes.return_value = ()
    return _WeakReferenceable(
        state_lock=threading.RLock(),
        config=Config(repo=REPO),
        state=SimpleNamespace(cached_scope_issues=[], cached_queue_issues=[], last_health_review_at=0.0),
        tech_lead_run_history=history,
        deps=SimpleNamespace(
            action_applier=SimpleNamespace(tech_lead_approvals=approvals, request_rework=None),
            services=SimpleNamespace(tech_lead_authority=InMemoryTechLeadAuthorityStore()),
            label_manager=LabelManager(Config(repo=REPO)),
            pending_work_claims=claims,
            standing_rulings=rulings_owner(),
            fact_gatherer=SimpleNamespace(board_publisher=None),
            action_liveness=SimpleNamespace(owner=SimpleNamespace(parked=lambda: ())),
            repository_host=MagicMock(),
            merge_hold_statuses=MergeHoldStatuses(host=MagicMock(), needs_human_label="needs-human"),
        ),
    )


def test_page_facade_reads_engine_state_only() -> None:
    """The page makes no GitHub call: the repository host is never touched."""
    from issue_orchestrator.control.label_manager import LabelManager
    from issue_orchestrator.infra.config import Config

    approvals = make_approvals()
    proposal = GitHubIssue(number=600, repo=REPO, title="p", labels=GATED, created_at="2026-10-01T00:00:00Z")
    approvals.record_scope((proposal,), {})
    host = MagicMock()
    history = MagicMock()
    history.recent.return_value = ()
    claims = MagicMock()
    claims.list_needs_human_causes.return_value = ()
    liveness = SimpleNamespace(owner=SimpleNamespace(parked=lambda: ()))
    orchestrator = _WeakReferenceable(
        state_lock=threading.RLock(),
        config=Config(repo=REPO),
        state=SimpleNamespace(cached_scope_issues=[], cached_queue_issues=[], last_health_review_at=0.0),
        tech_lead_run_history=history,
        deps=SimpleNamespace(
            action_applier=SimpleNamespace(tech_lead_approvals=approvals, request_rework=None),
            services=SimpleNamespace(tech_lead_authority=InMemoryTechLeadAuthorityStore()),
            label_manager=LabelManager(Config(repo=REPO)),
            pending_work_claims=claims,
            standing_rulings=rulings_owner(),
            fact_gatherer=SimpleNamespace(board_publisher=None),
            action_liveness=liveness,
            repository_host=host,
            merge_hold_statuses=MergeHoldStatuses(host=host, needs_human_label="needs-human"),
        ),
    )
    section = tech_lead_page_section(orchestrator)
    assert [item.number for item in section.waiting] == [600]
    assert host.mock_calls == []



def test_an_unobserved_scope_is_unavailable_never_an_all_clear(client, tmp_path) -> None:
    """#7763 review r10 F1: before its first complete approval-scope
    observation the engine answers 503, which the Control Center counts as
    not reporting; afterwards an empty scope is a real, empty section."""
    from issue_orchestrator.execution.control_center_tech_lead import ControlCenterTechLead
    from issue_orchestrator.infra.repo_registry import RegisteredRepo
    from issue_orchestrator.infra.tech_lead_proposal_facade import TechLeadPageNotObserved
    from issue_orchestrator.ports.repository_engine_supervisor import (
        MultiInstanceStatus,
        SupervisorStatus,
    )

    http, engine = client
    approvals = make_approvals()
    orchestrator = _page_engine(approvals)
    engine.tech_lead_page_section.side_effect = lambda: tech_lead_page_section(orchestrator)

    with pytest.raises(TechLeadPageNotObserved):
        tech_lead_page_section(orchestrator)
    response = http.get("/api/tech-lead/page")
    assert response.status_code == 503

    class _Transport:
        def read_section(self, port):
            answer = http.get("/api/tech-lead/page")
            return answer.json() if answer.status_code == 200 else None

    supervisor = MagicMock()
    supervisor.status_all_instances.side_effect = lambda path, *a, **k: MultiInstanceStatus(
        repo_root=str(path), instances=[SupervisorStatus(state="running", port=8001)])
    (tmp_path / "a").mkdir()
    cc = ControlCenterTechLead(supervisor, lambda: [RegisteredRepo(path=str(tmp_path / "a"), name="a")], _Transport())

    page = cc.page()
    assert page.unreported_count == 1 and page.waiting_count == 0

    approvals.record_scope((), {})  # the first observation: genuinely empty
    assert http.get("/api/tech-lead/page").status_code == 200
    assert cc.page().unreported_count == 0



def test_approving_a_declined_reopened_proposal_is_refused(client) -> None:
    """#7763 review r15 F2: Decline is final; a later Approve reports 409 and
    writes nothing, instead of a success the engine would never honour."""
    http, engine = client
    orchestrator, approvals, host = _engine(GitHubIssue(number=505, repo=REPO, title="p", labels=GATED))
    engine.request_tech_lead_proposal.side_effect = lambda command: proposal_command(orchestrator, command)
    assert http.post("/api/tech-lead/proposals", json={"proposal_issue_number": 505, "decision": "decline"}).status_code == 200
    host.issue = GitHubIssue(number=505, repo=REPO, title="p", labels=GATED, state="open")  # reopened
    host.writes.clear()

    response = http.post("/api/tech-lead/proposals", json={"proposal_issue_number": 505, "decision": "approve"})

    assert response.status_code == 409 and response.json()["outcome"] == "unavailable"
    assert host.writes == []
    assert approvals.records.load_operator_approval(505) is None
    assert approvals.verify(host.issue, fresh=True).kind is ApprovalVerdictKind.DECLINED



def test_a_known_proposal_stripped_of_everything_still_waits_on_the_page() -> None:
    """#7763 review r16 F1: the page counts a proposal by the owner's index."""
    approvals = make_approvals()
    approvals.remember_proposals([730])
    edited = GitHubIssue(number=730, repo=REPO, title="p", labels=("agent:backend",), body="edited")
    approvals.record_scope((edited,), {})

    section = tech_lead_page_section(_page_engine(approvals))

    assert section.waiting_count == 1 and [item.number for item in section.waiting] == [730]


def test_a_scope_marked_unavailable_after_a_success_is_unreported(client) -> None:
    """#7763 review r16 F2: a later failed refresh is 503 again, not 200/zero."""
    http, engine = client
    approvals = make_approvals()
    orchestrator = _page_engine(approvals)
    engine.tech_lead_page_section.side_effect = lambda: tech_lead_page_section(orchestrator)
    approvals.record_scope((), {})
    assert http.get("/api/tech-lead/page").status_code == 200

    approvals.mark_scope_unavailable()  # the next refresh started and failed

    assert http.get("/api/tech-lead/page").status_code == 503



@pytest.mark.parametrize("decision", ["approve", "decline"])
def test_a_command_for_another_engines_proposal_writes_nothing(client, decision) -> None:
    """#7763 review r24 F2: an engine scoped to run-a refuses Approve and
    Decline for a run-b proposal, before any write."""
    http, engine = client
    other = GitHubIssue(number=506, repo=REPO, title="p", labels=("run-b", *GATED))
    orchestrator, approvals, host = _engine(other, scope_label="run-a")
    engine.request_tech_lead_proposal.side_effect = lambda command: proposal_command(orchestrator, command)

    response = http.post("/api/tech-lead/proposals", json={"proposal_issue_number": 506, "decision": decision})

    assert response.status_code == 409 and response.json()["outcome"] == "unavailable"
    assert host.writes == []
    assert approvals.records.load_operator_approval(506) is None
    assert not approvals.is_declined(506)
