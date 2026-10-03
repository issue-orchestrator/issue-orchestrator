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
