"""The ONE owner of what a blocking label holds: work, or only a merge (#7678)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.control.human_gates import HumanGates, holds_merge, merge_decision_request
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.domain.human_block import HumanHoldScope, NeedsHumanCause, hold_scope
from issue_orchestrator.entrypoints.bootstrap import build_orchestrator_for_testing
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_tools import create_git
from issue_orchestrator.infra.config import Config

PR = 379
MERGE = frozenset({NeedsHumanCause.MERGE_DECISION})


def _gates(causes: frozenset[NeedsHumanCause] | Exception) -> HumanGates:
    def read(numbers):
        if isinstance(causes, Exception):
            raise causes
        return {number: causes for number in numbers}

    return HumanGates(labels=LabelManager(Config()), causes=read)


def test_only_merge_decisions_are_merge_scoped() -> None:
    assert [c for c in NeedsHumanCause if c.scope is HumanHoldScope.MERGE] == [NeedsHumanCause.MERGE_DECISION]
    assert hold_scope(MERGE) is HumanHoldScope.MERGE
    assert hold_scope(frozenset()) is HumanHoldScope.WORK  # a label put on by hand
    assert hold_scope(MERGE | {NeedsHumanCause.MERGE_ESCALATION}) is HumanHoldScope.WORK


def test_a_merge_hold_lets_the_prs_review_and_rework_through() -> None:
    gates = _gates(MERGE)

    assert gates.needs_human_scope(PR, ["needs-human", "code-reviewed"]) is HumanHoldScope.MERGE
    assert gates.work_blocking(PR, ["needs-human", "code-reviewed"]) == ()
    assert gates.work_blocking(PR, ["needs-human", "blocked-failed"]) == ("blocked-failed",)
    assert gates.holds_merge(["needs-human"]) is True


@pytest.mark.parametrize(
    ("causes", "labels"),
    [
        (frozenset({NeedsHumanCause.MERGE_ESCALATION}), ("needs-human",)),  # rework exhausted
        (frozenset({NeedsHumanCause.AGENT_COMPLETION}), ("needs-human",)),  # a pre-work question
        (frozenset(), ("needs-human",)),  # put on by hand
        (MERGE, ("needs-human", "tech-lead-needs-human")),  # handed over too
        (RuntimeError("cause store locked"), ("needs-human",)),
    ],
)
def test_every_other_needs_human_holds_the_work(causes, labels) -> None:
    gates = _gates(causes)

    assert gates.needs_human_scope(PR, labels) is HumanHoldScope.WORK
    assert "needs-human" in gates.work_blocking(PR, labels)


def test_an_issues_blocks_always_hold_its_work_with_no_cause_read() -> None:
    gates = _gates(RuntimeError("never read"))

    assert gates.issue_work_blocking(["needs-human", "pr-pending"]) == ("needs-human",)


def test_absent_needs_human_has_no_scope_and_holds_no_merge() -> None:
    assert _gates(MERGE).needs_human_scope(PR, ["code-reviewed"]) is None
    assert holds_merge(LabelManager(Config()), ["pr-pending"], ["code-reviewed"]) is False


def test_a_merge_hold_is_only_ever_asked_for_against_a_pr() -> None:
    request = merge_decision_request(PR, "why")
    assert (request.target, request.cause) == (PR, NeedsHumanCause.MERGE_DECISION)


@pytest.fixture
def orchestrator(tmp_path):
    create_git(LocalCommandRunner()).run(tmp_path, ["init", "-b", "main"])
    config = Config()
    config.repo = "test/repo"
    config.repo_root = tmp_path
    config.worktree_base = tmp_path / "worktrees"
    github = MagicMock()
    github.get_issue_labels.return_value = []
    github.get_issue_labels_fresh.return_value = []
    with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
        return build_orchestrator_for_testing(config=config, github=github)


def test_every_review_and_rework_path_reads_the_one_owner(orchestrator) -> None:
    """Discovery, rework scan, startup recovery, launch and the stuck sweep
    must type a block over the SAME shared record, or one of them keeps
    vetoing what another lets through."""
    block = orchestrator.deps.needs_human_block
    composed = (
        orchestrator.deps.pr_scanner._gates,
        orchestrator._startup_manager._human_gates,
        orchestrator._session_launcher._human_gates,
        orchestrator.deps.fact_gatherer.human_gates,
        orchestrator.deps.human_gates,
    )

    for gates in composed:
        assert isinstance(gates, HumanGates)
        assert gates.causes == block.recorded_causes
