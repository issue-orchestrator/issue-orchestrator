"""The engine's own label writes reach its issue cache before the next refresh (#8113).

Every engine label write ends at the repository adapter, which reports it to
the issue cache. These scenarios replay the exam run that found the defect:
a block removed in one tick was still granted a triage, and its removal
re-planned, from the stale cached labels; a block put on in one tick was
missing from the agenda of a review launched after it (#8094).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.adapters.github import GitHubAdapter
from issue_orchestrator.adapters.github.cache import GitHubCache
from issue_orchestrator.adapters.github.http_client import GitHubHttpError
from issue_orchestrator.control.actions import RemoveLabelAction
from issue_orchestrator.control.blocked_item_triage import OpenProposals, StateBlockedItemTriage
from issue_orchestrator.control.dependency_scope_label import plan_dependency_scope_labels
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.queue_cache import QueueCacheLabelWrites
from issue_orchestrator.control.scheduler import AvailabilityReason, IssueAvailabilityDecision
from issue_orchestrator.domain.models import Issue, OrchestratorState
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.verification import VerificationResult

ANCHOR = 8095
BLOCKED = 8089  # carried blocked-cross-milestone until tick 41 removed it
FRESH = 8088  # an agent's needs_human completion blocks it mid-tick


def _config() -> Config:
    config = Config()
    config.repo = "owner/repo"
    config.tech_lead_review_agent = "agent:tech-lead"
    return config


def _board() -> OrchestratorState:
    """The tick-start cache: what the last GitHub refresh returned."""
    issues = [
        Issue(number=FRESH, title="Split me", labels=["agent:backend"], repo="owner/repo"),
        Issue(
            number=BLOCKED, title="Cross-milestone", repo="owner/repo",
            labels=["agent:backend", "blocked-cross-milestone"],
        ),
        Issue(number=ANCHOR, title="Health review", labels=["agent:tech-lead"], repo="owner/repo"),
    ]
    return OrchestratorState(cached_scope_issues=list(issues), cached_queue_issues=list(issues))


def _engine_adapter(config: Config, state: OrchestratorState, http_client: MagicMock) -> GitHubAdapter:
    """The adapter as the composition root builds it: its writes reach ``state``."""
    verification = MagicMock()
    verification.verify_condition.return_value = (VerificationResult.SUCCESS, None)
    return GitHubAdapter(
        repo="owner/repo",
        config=config,
        cache=GitHubCache(default_ttl=60.0),
        verification_service=verification,
        http_client=http_client,
        label_writes=QueueCacheLabelWrites(config, state),
    )


class _NoTriageYet:
    def latest_triage_for_issue(self, issue_number: int) -> None:
        return None


class _EpisodesUnread:
    """Every episode unknown: an item is owed a triage on its labels alone."""

    def verified(self, issues):
        return {}


def _health_review(config: Config, state: OrchestratorState) -> StateBlockedItemTriage:
    """The agenda a health review launched now would be granted."""
    return StateBlockedItemTriage(
        config=config,
        state=lambda: state,
        labels=LabelManager(config),
        needs_human_causes=lambda numbers: {number: frozenset() for number in numbers},
        charter_ledger=_NoTriageYet(),
        open_proposals=lambda: OpenProposals(by_source={}, numbers=frozenset()),
        timeline_reader=lambda number, limit: [],
        standing_rulings=lambda number: (),
        episodes=_EpisodesUnread(),
    )


def _granted(config: Config, state: OrchestratorState) -> list[int]:
    agenda = _health_review(config, state).agenda(anchor_issue_number=ANCHOR)
    return [grant.issue_number for grant in agenda.grants]


@pytest.mark.parametrize(
    "github_answer",
    [None, GitHubHttpError("Not found", status_code=404)],
    ids=["removed", "already-gone"],
)
def test_a_block_removed_before_the_refresh_is_not_granted_a_triage(github_answer) -> None:
    config, state, http = _config(), _board(), MagicMock()
    http.remove_label.side_effect = github_answer
    assert _granted(config, state) == [BLOCKED]  # what the stale cache granted

    _engine_adapter(config, state, http).remove_label(BLOCKED, "blocked-cross-milestone")

    assert _granted(config, state) == []


def test_a_block_put_on_this_tick_is_on_the_agenda_of_a_review_launched_after_it() -> None:
    """#8094: the needs-human block owner writes through the same adapter."""
    config, state = _config(), _board()

    _engine_adapter(config, state, MagicMock()).add_label(FRESH, "needs-human")

    assert _granted(config, state) == [FRESH, BLOCKED]


def _next_tick_scope_label_plan(state: OrchestratorState) -> list:
    """What ``plan_dependency_scope_labels`` plans from the cached issues."""
    evaluator = MagicMock()
    evaluator.evaluate_work_gate.return_value = MagicMock(
        work_violates_milestone_scope=False, dependencies=[],
    )
    decisions = [
        IssueAvailabilityDecision(issue=issue, available=True, reason=AvailabilityReason.AVAILABLE)
        for issue in state.cached_queue_issues
    ]
    return plan_dependency_scope_labels(
        decisions, label_manager=LabelManager(_config()), evaluator=evaluator,
    )


def test_a_removal_is_not_re_planned_on_the_next_tick() -> None:
    config, state = _config(), _board()
    planned = _next_tick_scope_label_plan(state)
    assert [(a.issue_number, a.label) for a in planned if isinstance(a, RemoveLabelAction)] == [
        (BLOCKED, "blocked-cross-milestone")
    ]

    _engine_adapter(config, state, MagicMock()).remove_label(BLOCKED, "blocked-cross-milestone")

    assert _next_tick_scope_label_plan(state) == []


def test_a_failed_removal_leaves_the_block_on_the_agenda() -> None:
    """Only what GitHub now holds reaches the cache, never what was attempted."""
    config, state, http = _config(), _board(), MagicMock()
    http.remove_label.side_effect = GitHubHttpError("boom", status_code=500)

    with pytest.raises(GitHubHttpError):
        _engine_adapter(config, state, http).remove_label(BLOCKED, "blocked-cross-milestone")

    assert _granted(config, state) == [BLOCKED]
