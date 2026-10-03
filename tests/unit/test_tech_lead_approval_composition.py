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
