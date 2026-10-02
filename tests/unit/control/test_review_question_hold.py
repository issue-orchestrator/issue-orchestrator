"""The owner of which issue blocks a review may run over (#7593)."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.review_question_hold import AgentQuestionReviewHolds
from issue_orchestrator.domain.human_block import NeedsHumanCause
from issue_orchestrator.entrypoints.bootstrap import build_orchestrator_for_testing
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_tools import create_git
from issue_orchestrator.infra.config import Config

ISSUE = 364


@dataclass(frozen=True)
class _Issue:
    number: int
    labels: tuple[str, ...]


class _Causes:
    def __init__(self, causes: frozenset[NeedsHumanCause] | Exception) -> None:
        self._causes = causes

    def recorded_causes(self, issue_numbers):
        if isinstance(self._causes, Exception):
            raise self._causes
        return {number: self._causes for number in issue_numbers}


def _holds(causes: frozenset[NeedsHumanCause] | Exception) -> AgentQuestionReviewHolds:
    return AgentQuestionReviewHolds(_Causes(causes), LabelManager(Config()))  # type: ignore[arg-type]


def test_an_agents_own_question_is_admitted() -> None:
    holds = _holds(frozenset({NeedsHumanCause.AGENT_COMPLETION}))

    assert holds.review_admitted_blocks(_Issue(ISSUE, ("needs-human", "pr-pending"))) == {
        "needs-human"
    }


@pytest.mark.parametrize(
    ("causes", "labels"),
    [
        (frozenset({NeedsHumanCause.AGENT_COMPLETION}), ("pr-pending",)),  # no block at all
        (frozenset(), ("needs-human",)),  # operator intent: no recorded cause
        (frozenset({NeedsHumanCause.SESSION_LIFECYCLE}), ("needs-human",)),
        (
            frozenset({NeedsHumanCause.AGENT_COMPLETION, NeedsHumanCause.ACTION_LIVENESS}),
            ("needs-human",),
        ),
        (
            frozenset({NeedsHumanCause.AGENT_COMPLETION}),
            ("needs-human", "tech-lead-needs-human"),
        ),
        (RuntimeError("cause store locked"), ("needs-human",)),
    ],
)
def test_nothing_else_is_admitted(causes, labels) -> None:
    assert _holds(causes).review_admitted_blocks(_Issue(ISSUE, labels)) == frozenset()


def test_a_missing_issue_admits_nothing() -> None:
    assert _holds(frozenset({NeedsHumanCause.AGENT_COMPLETION})).review_admitted_blocks(None) == frozenset()


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


def test_every_review_path_is_composed_with_the_one_policy(orchestrator) -> None:
    """Discovery, startup recovery and launch must ask the same owner, over the
    same shared needs-human block, or one of them keeps vetoing the review."""
    block = orchestrator.deps.needs_human_block
    composed = (
        orchestrator.deps.pr_scanner._review_question_holds,
        orchestrator._startup_manager._review_question_holds,
        orchestrator._session_launcher._review_question_holds,
    )

    for holds in composed:
        assert isinstance(holds, AgentQuestionReviewHolds)
        assert holds.block is block
