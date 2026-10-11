"""The composition root wires ONE approval owner everywhere it is read (#7763)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.control.tech_lead_approval import TechLeadApprovals
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.entrypoints.bootstrap import build_orchestrator_for_testing
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_tools import create_git
from issue_orchestrator.infra.config import Config


@pytest.fixture
def orchestrator(tmp_path):
    create_git(LocalCommandRunner()).run(tmp_path, ["init", "-b", "main"])
    config = Config()
    config.repo = "test/repo"
    config.repo_root = tmp_path
    config.worktree_base = tmp_path / "worktrees"
    config.tech_lead_review_agent = "agent:tech-lead"
    with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
        return build_orchestrator_for_testing(config=config, github=MagicMock())


def test_one_owner_backs_verification_consent_settlement_and_admission(orchestrator) -> None:
    approvals = orchestrator.deps.fact_gatherer.approvals

    assert isinstance(approvals, TechLeadApprovals)
    assert orchestrator.deps.action_applier.tech_lead_approvals is approvals
    # The scheduler asks the same owner, so an approval verified by the fact
    # scan is the one that admits the item.
    forged = Issue(number=7, title="t", labels=["tech-lead-proposal", "approved"])
    assert orchestrator.deps.planner.scheduler.approval_admission(forged) is False
    approvals._verified[7] = MagicMock(approved=True)  # verified by a scan
    assert orchestrator.deps.planner.scheduler.approval_admission(forged) is True


def test_operator_approvals_persist_in_the_authority_store(orchestrator) -> None:
    approvals = orchestrator.deps.fact_gatherer.approvals

    assert approvals.records is orchestrator.deps.services.tech_lead_authority.operator_approvals
    # ...and so does the proposal index the approval scope reads (#7763 r6 F2).
    authority = orchestrator.deps.services.tech_lead_authority
    assert approvals.index is authority.proposal_index
    # ...and the op ledger is part of proposal identity (r19 F1).
    from issue_orchestrator.domain.tech_lead_session import StoredTechLeadOp

    authority.record_op(issue_number=77, op=StoredTechLeadOp(
        op_type="reset_retry", target_issue_number=13, rationale="r", source_run_id="run",
        source_session_name="s", source_action_id="A1", created_at="2026-10-03T00:00:00Z"))
    approvals.remember_proposals([78])  # drops the cached identity
    assert {77, 78} <= approvals.known_proposals()


def test_the_sqlite_proposal_index_survives_a_restart(tmp_path) -> None:
    from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore

    store = SqliteTechLeadAuthorityStore(tmp_path / "authority.db")
    store.proposal_index.index_proposals([5, 7, 7, 9])
    store.proposal_index.retire_proposals([5])
    store.proposal_index.decline_proposals([9])
    store.proposal_index.retire_proposals([9])  # a declined row is kept for good

    reopened = SqliteTechLeadAuthorityStore(tmp_path / "authority.db")

    assert reopened.proposal_index.indexed_proposals() == {7}
    assert reopened.proposal_index.declined_proposals() == {9}
    # A retired (closed) proposal is inactive, never forgotten (r18 F1)...
    assert reopened.proposal_index.known_proposals() == {5, 7, 9}
    reopened.proposal_index.index_proposals([5])  # ...and seen open again
    assert reopened.proposal_index.indexed_proposals() == {5, 7}


def test_a_goal_pilot_label_action_can_never_write_an_approval(orchestrator) -> None:
    """#7763 review r9 F1: a generic label action through the applier, with an
    engine token whose account is a maintainer, must not produce an `approved`
    event (it would read as that maintainer's approval)."""
    from issue_orchestrator.control.goal_pilot import GoalPilot

    github = orchestrator.deps.repository_host
    github.add_label.reset_mock()
    github.remove_label.reset_mock()
    github.has_label.side_effect = lambda number, label: label == "awaiting-approval"
    pilot = GoalPilot(store=MagicMock(), events=MagicMock(), action_applier=orchestrator.deps.action_applier)

    for labels in ({"labels_add": ["approved"]}, {"labels_remove": ["awaiting-approval"]}):
        outcome = pilot.execute_action(
            "run-1", {"action_type": "dispatch", "issue_number": 500, **labels}, github
        )
        assert outcome["status"] == "failed"
    created = pilot.execute_action(
        "run-1", {"action_type": "create_issue", "title": "t", "labels": ["tech-lead-proposal"]}, github
    )

    assert created["status"] == "failed"
    github.add_label.assert_not_called()
    github.remove_label.assert_not_called()
    github.create_issue.assert_not_called()



def test_the_real_engine_serves_its_tech_lead_page_section(orchestrator) -> None:
    """#7763 review r20 F2: the page runs against the real (unhashable)
    Orchestrator; its merge-hold reader is the engine's own, built once."""
    orchestrator.deps.action_applier.tech_lead_approvals.record_scope((), {})

    section = orchestrator.tech_lead_page_section()

    assert section.waiting_count == 0 and section.waiting == []
    assert orchestrator.deps.merge_hold_statuses is orchestrator.deps.merge_hold_statuses


def test_a_decline_by_another_engine_sharing_the_store_stops_execution_at_once(tmp_path) -> None:
    """#7763 review r24 F1: consent reads the declined disposition fresh from
    the shared store, never one owner's cache."""
    from issue_orchestrator.control.tech_lead_approval import TechLeadApprovals
    from issue_orchestrator.control.tech_lead_proposal_execution import execute_approved_tech_lead_op
    from issue_orchestrator.domain.tech_lead_approval import with_proposal_marker
    from issue_orchestrator.domain.tech_lead_session import StoredTechLeadOp
    from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
    from tests.approval_helpers import CLAIMED, FakeApprovalEvidence
    from types import SimpleNamespace

    evidence = FakeApprovalEvidence()
    evidence.label(5)  # a maintainer's standing approval

    def owner(store):
        return TechLeadApprovals(evidence, store.operator_approvals, store.proposal_index,
                                 lambda: (n for n, _op in store.list_ops()))

    store_a = SqliteTechLeadAuthorityStore(tmp_path / "authority.db")
    store_b = SqliteTechLeadAuthorityStore(tmp_path / "authority.db")
    store_a.record_op(issue_number=5, op=StoredTechLeadOp(
        op_type="reset_retry", target_issue_number=13, rationale="r", source_run_id="run",
        source_session_name="s", source_action_id="A1", created_at="2026-10-03T00:00:00Z"))
    a, b = owner(store_a), owner(store_b)
    proposal = Issue(number=5, title="t", labels=list(CLAIMED), state="open", repo="o/r",
                     body=with_proposal_marker("b"))
    assert a.verify(proposal).approved and a.declined_numbers() == frozenset()  # A's caches warm

    b.decline(5)  # B's first durable write; B then crashes before closing
    host = MagicMock()
    host.get_issue.return_value = proposal  # still open and labelled approved
    apply_fn = MagicMock()

    result = execute_approved_tech_lead_op(
        SimpleNamespace(proposal_issue_number=5), apply_fn, repository_host=host, ops=store_a, approvals=a,
    )

    assert not result.success
    apply_fn.assert_not_called()
    assert store_a.load_op(issue_number=5) is not None



def test_a_self_routed_promotion_is_a_proposal_from_its_ledger_row(orchestrator) -> None:
    """#7763 review r25 F2: the promotion ledger names a promoted finding
    filed in this repository a proposal before (or without) any index row or
    labels; one routed to another repository is not this engine's."""
    from issue_orchestrator.domain.tech_lead_findings import PromotedFinding

    authority = orchestrator.deps.services.tech_lead_authority
    approvals = orchestrator.deps.fact_gatherer.approvals
    authority.record_promotion(promotion=PromotedFinding(
        signature="sig-a", case_file_issue_number=9, target_repo="test/repo", target_issue_number=810))
    authority.record_promotion(promotion=PromotedFinding(
        signature="sig-b", case_file_issue_number=9, target_repo="other/repo", target_issue_number=811))
    stripped = Issue(number=810, title="t", labels=["agent:backend"], body="edited")

    assert approvals.is_known(810) and not approvals.is_known(811)
    assert orchestrator.deps.planner.scheduler.approval_admission(stripped) is False


def test_a_follow_up_filed_after_the_caches_warmed_is_known_at_once(tmp_path) -> None:
    """#7763 review r25 F1: identity is read fresh, so a proposal the filing
    boundary indexed after this owner cached its sets still counts."""
    from issue_orchestrator.control.tech_lead_approval import TechLeadApprovals, unapproved_proposal_launch
    from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
    from tests.approval_helpers import FakeApprovalEvidence

    store = SqliteTechLeadAuthorityStore(tmp_path / "authority.db")
    approvals = TechLeadApprovals(FakeApprovalEvidence(), store.operator_approvals, store.proposal_index, lambda: ())
    assert approvals.known_proposals() == frozenset() and approvals.indexed_proposals() == frozenset()

    store.proposal_index.index_proposals([820])  # what the creation boundary writes at filing
    stripped = Issue(number=820, title="t", labels=["agent:backend"], body="edited")
    host = MagicMock()
    host.get_issue.return_value = stripped

    assert not approvals.admits(stripped)
    assert unapproved_proposal_launch(820, host, approvals) is not None
