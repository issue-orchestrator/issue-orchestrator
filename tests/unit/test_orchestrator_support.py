"""Unit tests for OrchestratorSupport.

These tests verify the behavior of support functions extracted from the Orchestrator.
Tests are behavior-centric, focusing on invariants, state transitions, and outcomes.
"""

import time
import pytest
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch, PropertyMock
from typing import Optional

from issue_orchestrator.control.orchestrator_support import (
    OrchestratorSupport,
    log_transition,
    init_orchestrator_components,
    pause_issue_for_reconciliation,
    clear_discovered_facts,
    emit_heartbeat_if_needed,
    check_health,
    run_planning_cycle,
    run_tick,
    _fetch_and_update_queue,
    _iter_blocked_history_issue_numbers,
    _record_issue_refreshes,
    _reconcile_closed_issue_history,
    _select_hot_issue_numbers,
    _track_stale_ticks,
)
from issue_orchestrator.adapters.github.http_client import GitHubHttpError
from issue_orchestrator.control.issue_fetch_resilience import (
    IssueFetchResilience,
    PermanentIssueFetchError,
    TransientIssueFetchError,
)
from issue_orchestrator.control.reconciliation import (
    ReconciliationRequired,
    ExpectedState,
    ExternalSnapshot,
    get_pause_label,
)
from issue_orchestrator.control.session_history import CLOSED_ISSUE_HISTORY_STATUS_REASON
from issue_orchestrator.control.actions import (
    ActionResult,
    ActionType,
    AddLabelAction,
    LaunchSessionAction,
    SessionType,
)
from issue_orchestrator.control.health_gate import HealthGate, HealthDecision
from issue_orchestrator.domain.models import (
    Issue,
    Session,
    SessionStatus,
    OrchestratorState,
    PendingReview,
    PendingRework,
    PendingTriageReview,
    PendingCleanup,
    DiscoveredAwaitingMergeDrift,
    DiscoveredAwaitingMergeEscalation,
    DiscoveredAwaitingMergeReconciliation,
    DiscoveredMergeQueueEnqueue,
    DiscoveredRetrospectiveReview,
    DiscoveredReview,
    DiscoveredRework,
    DiscoveredEscalation,
    DiscoveredFailure,
    AgentConfig,
    ImmediateCleanup,
    SessionHistoryEntry,
    SessionHistoryStatus,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from tests.unit.session_run_helpers import make_session_run_assets
from issue_orchestrator.events import EventName
from issue_orchestrator.ports import TraceEvent
from issue_orchestrator.infra.config import Config


# =============================================================================
# Test Fixtures and Helpers
# =============================================================================


@pytest.fixture
def mock_event_sink():
    """Create a mock EventSink for tracking published events."""
    sink = MagicMock()
    sink.events = []

    def capture_publish(event):
        sink.events.append(event)

    sink.publish = Mock(side_effect=capture_publish)
    return sink


@pytest.fixture
def mock_repository_host():
    """Create a mock RepositoryHost."""
    host = MagicMock()
    host.add_label = Mock()
    host.remove_label = Mock()
    host.create_issue_key = Mock(side_effect=lambda n: FakeIssueKey(name=str(n)))
    return host


@pytest.fixture
def mock_action_applier():
    """Create a mock ActionApplier."""
    applier = MagicMock()
    applier.apply = Mock()
    return applier


@pytest.fixture
def sample_orchestrator_state():
    """Create a sample OrchestratorState for testing."""
    return OrchestratorState(
        active_sessions=[],
        paused=False,
        pending_reviews=[],
        pending_reworks=[],
        pending_triage_reviews=[],
        discovered_reviews=[],
        discovered_reworks=[],
        discovered_escalations=[],
        discovered_failures=[],
    )


@pytest.fixture
def sample_event_context():
    """Create a mock EventContext."""
    ctx = MagicMock()
    ctx.tick_id = 1
    ctx.enrich = Mock(side_effect=lambda d: d)
    return ctx


@pytest.fixture
def sample_agent_config(tmp_path):
    """Create a sample AgentConfig."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Test prompt")
    return AgentConfig(
        prompt_path=prompt_path,
        model="sonnet",
        timeout_minutes=45,
    )


def make_issue(number: int, title: str = "Test Issue", labels: list | None = None) -> Issue:
    """Create a test issue."""
    return Issue(
        number=number,
        title=title,
        labels=labels or [],
        state="open",
    )


def make_history_entry(issue_number: int, status: SessionHistoryStatus) -> SessionHistoryEntry:
    """Create a test session history entry."""
    return SessionHistoryEntry(
        issue_number=issue_number,
        title=f"Issue {issue_number}",
        agent_type="agent:web",
        status=status,
        runtime_minutes=1,
        pr_url=None,
        status_reason=status,
        completed_at=datetime.now(),
    )


def make_session(issue: Issue, task: TaskKind = TaskKind.CODE, tmp_path: Path = None) -> Session:
    """Create a test session for an issue."""
    issue_key = FakeIssueKey(name=str(issue.number))
    session_key = SessionKey(issue=issue_key, task=task)
    worktree = tmp_path or Path("/tmp/test-worktree")

    return Session(
        key=session_key,
        issue=issue,
        agent_config=AgentConfig(
            prompt_path=Path("/tmp/prompt.md"),
        ),
        terminal_id=f"session-{issue.number}",
        worktree_path=worktree,
        branch_name=f"issue-{issue.number}",
        run_assets=make_session_run_assets(
            worktree,
            session_name=f"session-{issue.number}",
        ),
        started_at=datetime.now(),
        status=SessionStatus.RUNNING,
    )


class TestQueueFetchPlanner:
    """Tests for queue fetch-layer planner behavior."""

    def _make_config(self) -> Config:
        config = Config()
        config.queue_refresh_seconds = 600
        config.fetch_layer_enabled = True
        config.fetch_layer_network_sync_seconds = 60
        config.fetch_layer_full_scan_interval_seconds = 3600
        config.fetch_layer_discovery_limit = 10
        config.fetch_layer_max_hot_issues_per_cycle = 10
        config.fetch_layer_pr_scan_every_n_refreshes = 2
        config.fetch_layer_dependency_scan_every_n_refreshes = 1
        return config

    def test_manual_refresh_uses_full_scan(self, mock_event_sink, mock_repository_host):
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
        )
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.fetch_all_issues.return_value = [make_issue(2, labels=["agent:web"])]

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=True,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.fetch_all_issues.assert_called_once()
        github_workflow.refresh_issues.assert_not_called()
        assert state.queue_last_refresh_mode == "full"
        assert state.queue_refresh_count == 1
        assert state.queue_last_full_scan_at > 0

    def test_scheduled_refresh_uses_incremental_when_full_scan_not_due(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"]), make_issue(2, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
        )
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, title="Updated 1", labels=["agent:web"])]
        github_workflow.fetch_discovery_issues.return_value = [make_issue(3, labels=["agent:web"])]

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.fetch_all_issues.assert_not_called()
        github_workflow.refresh_issues.assert_called_once()
        github_workflow.fetch_discovery_issues.assert_called_once_with(config.filtering.milestone, 10)
        assert state.queue_last_refresh_mode == "incremental"
        assert state.queue_refresh_count == 1
        queue_numbers = {issue.number for issue in state.cached_queue_issues}
        assert queue_numbers == {1, 2, 3}

    def test_scan_cadence_skips_pr_scans_until_due(self, mock_event_sink, mock_repository_host):
        config = self._make_config()
        config.fetch_layer_pr_scan_every_n_refreshes = 3
        config.fetch_layer_dependency_scan_every_n_refreshes = 2
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            queue_refresh_count=1,  # next refresh count = 2
        )
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_discovery_issues.return_value = []

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.scan_needs_code_review_prs.assert_not_called()
        github_workflow.scan_needs_rework_prs.assert_not_called()
        github_workflow.scan_pending_pr_work.assert_called_once_with(
            state,
            include_general_scans=False,
        )
        scheduler.get_available_issues.assert_called_once()

    def test_pr_scan_uses_single_workflow_call_when_due(self, mock_event_sink, mock_repository_host):
        config = self._make_config()
        config.fetch_layer_pr_scan_every_n_refreshes = 2
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            queue_refresh_count=1,  # next refresh count = 2
        )
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_discovery_issues.return_value = []

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.scan_pending_pr_work.assert_called_once_with(
            state,
            include_general_scans=True,
        )
        github_workflow.scan_needs_code_review_prs.assert_not_called()
        github_workflow.scan_needs_rework_prs.assert_not_called()

    def test_visibility_aware_hot_list_prioritizes_visible_issues(self, mock_event_sink, mock_repository_host):
        config = self._make_config()
        config.fetch_layer_visibility_aware_enabled = True
        config.fetch_layer_max_hot_issues_per_cycle = 2
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"]), make_issue(2, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            ui_visible_issue_numbers=[99],
            ui_visible_updated_at=time.time(),
        )
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_discovery_issues.return_value = []

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.refresh_issues.assert_called_once_with([99, 1])

    def test_selective_sync_planner_can_skip_noncritical_scans(self, mock_event_sink, mock_repository_host):
        config = self._make_config()
        config.fetch_layer_selective_sync_planner_enabled = True
        config.fetch_layer_pr_scan_every_n_refreshes = 10
        config.fetch_layer_dependency_scan_every_n_refreshes = 10
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            queue_refresh_count=1,
        )
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_discovery_issues.return_value = []

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.scan_needs_code_review_prs.assert_not_called()
        github_workflow.scan_needs_rework_prs.assert_not_called()
        github_workflow.scan_pending_pr_work.assert_called_once_with(
            state,
            include_general_scans=False,
        )
        scheduler.get_available_issues.assert_not_called()

    def test_fetch_prunes_refresh_timestamps_for_issues_no_longer_tracked(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            issue_refresh_timestamps={1: 100.0, 999: 200.0},
        )
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_discovery_issues.return_value = []

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        assert 1 in state.issue_refresh_timestamps
        assert 999 not in state.issue_refresh_timestamps

    def test_delta_sync_updates_watermark_only_after_successful_fetch(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            queue_delta_watermark="2026-01-01T00:00:00Z",
        )
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_delta_issues.return_value = (
            [make_issue(2, labels=["agent:web"])],
            "2026-01-01T01:00:00Z",
        )
        github_workflow.issue_in_scope.return_value = True

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.fetch_delta_issues.assert_called_once_with(
            since="2026-01-01T00:00:00Z",
            fetch_limit=10,
        )
        assert state.queue_delta_watermark == "2026-01-01T01:00:00Z"

    def test_delta_sync_error_propagates_without_mutating_queue(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            queue_delta_watermark="2026-01-01T00:00:00Z",
        )
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_delta_issues.side_effect = GitHubHttpError(
            "GitHub unavailable",
            status_code=503,
        )

        # The issue-list fetch is guarded, so a transient 503 surfaces as a
        # TransientIssueFetchError (with the raw GitHubHttpError as its cause)
        # rather than the raw error — but the queue must still be untouched.
        with pytest.raises(TransientIssueFetchError) as exc_info:
            _fetch_and_update_queue(
                config=config,
                events=mock_event_sink,
                state=state,
                repository_host=mock_repository_host,
                scheduler=scheduler,
                github_workflow=github_workflow,
                refresh_requested=False,
                inflight_stable_ids={},
                issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            )

        assert isinstance(exc_info.value.__cause__, GitHubHttpError)
        assert exc_info.value.__cause__.status_code == 503
        assert [issue.number for issue in state.cached_queue_issues] == [1]
        assert state.queue_delta_watermark == "2026-01-01T00:00:00Z"
        assert state.queue_refresh_count == 0
        assert state.queue_refresh_in_progress is False

    def test_post_fetch_pr_scan_error_is_not_classified_as_issue_fetch_failure(
        self, mock_event_sink, mock_repository_host
    ):
        """A RepositoryHostError from a *post-fetch* PR scan must surface as the
        raw error — not be reclassified as an issue-list fetch failure.

        The resilience guard wraps only the issue-list fetch. If it wrapped the
        whole queue update, this 404 from ``scan_pending_pr_work`` would be
        promoted to a PermanentIssueFetchError at tolerance 1 and shut the
        orchestrator down as if the repository were missing.
        """
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
        )
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        # The issue-list fetch itself succeeds...
        github_workflow.fetch_all_issues.return_value = [make_issue(1, labels=["agent:web"])]
        # ...but the downstream PR scan hits a repository-host 404.
        github_workflow.scan_pending_pr_work.side_effect = GitHubHttpError(
            "GitHub GET /repos/owner/repo/pulls failed: 404", status_code=404
        )
        # Tolerance 1 means the *first* issue-fetch 404 would fail fast — so if
        # the PR-scan 404 were (wrongly) routed through the policy, this would
        # raise PermanentIssueFetchError instead of the raw error.
        resilience = IssueFetchResilience("owner/repo", repo_not_found_tolerance=1)

        with pytest.raises(GitHubHttpError) as exc_info:
            _fetch_and_update_queue(
                config=config,
                events=mock_event_sink,
                state=state,
                repository_host=mock_repository_host,
                scheduler=scheduler,
                github_workflow=github_workflow,
                refresh_requested=True,  # manual → full scan → PR scan runs
                inflight_stable_ids={},
                issue_fetch_resilience=resilience,
            )

        # Raw repository error surfaces, NOT a resilience classification.
        assert exc_info.value.status_code == 404
        assert not isinstance(exc_info.value, TransientIssueFetchError)
        assert not isinstance(exc_info.value, PermanentIssueFetchError)
        github_workflow.scan_pending_pr_work.assert_called_once_with(
            state,
            include_general_scans=True,
        )
        # The fetch succeeded, so the policy recorded success: a *genuine*
        # issue-fetch 404 afterwards is still the first in its streak (proving
        # the PR-scan 404 was never counted by the policy).
        verdict = resilience.record_failure(
            GitHubHttpError("issues 404", status_code=404)
        )
        assert verdict.consecutive_repo_not_found == 1

    def test_delta_sync_removes_out_of_scope_issue_from_cached_queue(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(7, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            queue_delta_watermark="2026-01-01T00:00:00Z",
        )
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(7, labels=["agent:web"])]
        github_workflow.fetch_delta_issues.return_value = (
            [make_issue(7, labels=["agent:web"], title="No longer scoped")],
            "2026-01-01T01:00:00Z",
        )
        github_workflow.issue_in_scope.return_value = False

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        assert all(issue.number != 7 for issue in state.cached_queue_issues)

    def test_run_planning_cycle_uses_network_sync_interval_not_queue_refresh(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        config.queue_refresh_seconds = 0
        config.fetch_layer_network_sync_seconds = 120
        state = OrchestratorState()
        fact_gatherer = Mock()
        fact_gatherer.create_snapshot.return_value = Mock()
        planner = Mock()
        planner.plan.return_value = Mock(action_count=0, actions=[])
        github_workflow = Mock()

        last_sync = time.time()
        next_sync, refresh_requested = run_planning_cycle(
            config=config,
            events=mock_event_sink,
            event_context=Mock(enrich=lambda payload: payload),
            state=state,
            fact_gatherer=fact_gatherer,
            planner=planner,
            repository_host=mock_repository_host,
            scheduler=Mock(),
            github_workflow=github_workflow,
            apply_plan_fn=Mock(),
            clear_discovered_facts_fn=Mock(),
            last_network_sync=last_sync,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        assert next_sync == last_sync
        assert refresh_requested is False
        github_workflow.fetch_all_issues.assert_not_called()

    def test_run_planning_cycle_rechecks_pending_queue_shrink_after_delay(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        config.queue_refresh_seconds = 0
        config.fetch_layer_network_sync_seconds = 3600
        prior_issues = [
            make_issue(number, labels=["agent:web"]) for number in range(1, 21)
        ]
        state = OrchestratorState(
            cached_scope_issues=list(prior_issues),
            cached_queue_issues=list(prior_issues),
            queue_pending_shrink_missing_issue_numbers=list(range(2, 21)),
            queue_pending_shrink_confirm_at=1.0,
            queue_last_full_scan_at=time.time(),
        )
        fact_gatherer = Mock()
        fact_gatherer.create_snapshot.return_value = Mock()
        planner = Mock()
        planner.plan.return_value = Mock(action_count=0, actions=[])
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = list(prior_issues[1:11])
        github_workflow.fetch_discovery_issues.return_value = []
        last_sync = time.time()

        next_sync, refresh_requested = run_planning_cycle(
            config=config,
            events=mock_event_sink,
            event_context=Mock(enrich=lambda payload: payload),
            state=state,
            fact_gatherer=fact_gatherer,
            planner=planner,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            apply_plan_fn=Mock(),
            clear_discovered_facts_fn=Mock(),
            last_network_sync=last_sync,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        assert next_sync >= last_sync
        assert state.queue_last_network_sync_at >= last_sync
        assert refresh_requested is False
        assert state.queue_last_refresh_mode == "incremental"
        github_workflow.fetch_all_issues.assert_not_called()
        github_workflow.refresh_issues.assert_called_once_with(list(range(2, 12)))
        assert state.queue_pending_shrink_missing_issue_numbers == []

    def test_run_planning_cycle_survives_transient_fetch_failure(
        self, mock_event_sink, mock_repository_host
    ):
        """A transient issue-list failure keeps the cached queue and keeps going."""
        config = self._make_config()
        cached = [make_issue(1, labels=["agent:web"]), make_issue(2, labels=["agent:web"])]
        state = OrchestratorState(
            cached_queue_issues=list(cached),
            queue_last_full_scan_at=time.time(),
        )
        fact_gatherer = Mock()
        fact_gatherer.create_snapshot.return_value = Mock()
        planner = Mock()
        planner.plan.return_value = Mock(action_count=0, actions=[])
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.fetch_all_issues.side_effect = GitHubHttpError(
            "GitHub GET /repos/owner/repo/issues failed: 404", status_code=404
        )

        # Must NOT raise — the orchestrator stays up on a recoverable blip.
        run_planning_cycle(
            config=config,
            events=mock_event_sink,
            event_context=Mock(enrich=lambda payload: payload),
            state=state,
            fact_gatherer=fact_gatherer,
            planner=planner,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            apply_plan_fn=Mock(),
            clear_discovered_facts_fn=Mock(),
            last_network_sync=0.0,
            refresh_requested=True,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.fetch_all_issues.assert_called_once()
        # Cached queue is preserved, and planning still proceeds with it.
        assert [issue.number for issue in state.cached_queue_issues] == [1, 2]
        planner.plan.assert_called_once()

    def test_run_planning_cycle_fails_fast_on_permanent_fetch_failure(
        self, mock_event_sink, mock_repository_host
    ):
        """A persistent repo-not-found propagates a clear, actionable error."""
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
        )
        fact_gatherer = Mock()
        planner = Mock()
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.fetch_all_issues.side_effect = GitHubHttpError(
            "GitHub GET /repos/owner/repo/issues failed: 404", status_code=404
        )

        with pytest.raises(PermanentIssueFetchError) as exc_info:
            run_planning_cycle(
                config=config,
                events=mock_event_sink,
                event_context=Mock(enrich=lambda payload: payload),
                state=state,
                fact_gatherer=fact_gatherer,
                planner=planner,
                repository_host=mock_repository_host,
                scheduler=scheduler,
                github_workflow=github_workflow,
                apply_plan_fn=Mock(),
                clear_discovered_facts_fn=Mock(),
                last_network_sync=0.0,
                refresh_requested=True,
                inflight_stable_ids={},
                issue_fetch_resilience=IssueFetchResilience(
                    "owner/repo", repo_not_found_tolerance=1
                ),
            )

        assert "owner/repo" in str(exc_info.value)
        # Planning is never reached when the fetch fails fast.
        planner.plan.assert_not_called()

    def test_suspicious_full_scan_shrink_retains_queue_and_watermark(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        state = OrchestratorState(
            cached_scope_issues=[
                make_issue(number, labels=["agent:web"]) for number in range(1, 21)
            ],
            cached_queue_issues=[
                make_issue(number, labels=["agent:web"]) for number in range(1, 21)
            ],
            queue_delta_watermark="2026-01-01T00:00:00Z",
            queue_last_full_scan_at=time.time(),
        )
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.fetch_all_issues.return_value = [
            make_issue(1, labels=["agent:web"])
        ]
        queue_cache_store = Mock()

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=True,
            inflight_stable_ids={},
            queue_cache_store=queue_cache_store,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        assert [issue.number for issue in state.cached_queue_issues] == list(range(1, 21))
        assert [issue.number for issue in state.cached_scope_issues] == list(range(1, 21))
        assert state.queue_pending_shrink_missing_issue_numbers == list(range(2, 21))
        assert state.queue_delta_watermark == "2026-01-01T00:00:00Z"
        queue_cache_store.save_snapshot.assert_called_once_with(
            state.cached_scope_issues,
            "2026-01-01T00:00:00Z",
            repo=config.repo or "",
        )

    def test_fetch_logs_gh_cost_per_cycle(self, mock_event_sink, mock_repository_host, caplog):
        import logging

        caplog.set_level(logging.INFO)
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
        )
        scheduler = Mock()
        scheduler.get_available_issues.return_value = ([], [])
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_discovery_issues.return_value = []

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        assert "[FETCH-COST]" in caplog.text

    def test_blocked_history_issues_are_hot_refresh_candidates(self):
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            session_history=[
                make_history_entry(270, "needs_human"),
                make_history_entry(271, "completed"),
            ],
        )

        hot_numbers = _select_hot_issue_numbers(
            state,
            limit=10,
            visibility_aware_enabled=False,
        )

        assert 270 in hot_numbers
        assert 271 not in hot_numbers

    def test_live_pending_work_has_hot_refresh_priority_over_blocked_history(self):
        state = OrchestratorState(
            pending_reviews=[
                PendingReview(
                    issue_key=FakeIssueKey("270"),
                    pr_number=6073,
                    pr_url="https://github.com/test/repo/pull/6073",
                    branch_name="feature/270",
                    _issue_number=270,
                ),
            ],
            session_history=[make_history_entry(271, "needs_human")],
        )

        hot_numbers = _select_hot_issue_numbers(
            state,
            limit=1,
            visibility_aware_enabled=False,
        )

        assert hot_numbers == [270]

    def test_blocked_history_hot_refresh_candidates_are_deduplicated(self):
        state = OrchestratorState(
            session_history=[
                make_history_entry(270, "blocked"),
                make_history_entry(270, "needs_human"),
                make_history_entry(271, "needs_human"),
            ],
        )

        assert list(_iter_blocked_history_issue_numbers(state)) == [271, 270]

    def test_closed_issue_refresh_reconciles_blocked_history(self):
        closed_issue = make_issue(270, labels=["agent:web"])
        closed_issue.state = "closed"
        history_entry = make_history_entry(270, "needs_human")
        state = OrchestratorState(session_history=[history_entry])

        mutations = _reconcile_closed_issue_history(state, [closed_issue])

        assert len(mutations) == 1
        assert mutations[0].previous_status == "needs_human"
        assert history_entry.status == "closed"
        assert history_entry.status_reason == CLOSED_ISSUE_HISTORY_STATUS_REASON

    def test_open_issue_refresh_does_not_reconcile_blocked_history(self):
        open_issue = make_issue(270, labels=["agent:web"])
        history_entry = make_history_entry(270, "needs_human")
        state = OrchestratorState(session_history=[history_entry])

        mutations = _reconcile_closed_issue_history(state, [open_issue])

        assert mutations == []
        assert history_entry.status == "needs_human"


# =============================================================================
# Tests for log_transition
# =============================================================================


class TestLogTransition:
    """Tests for the log_transition helper function."""

    def test_logs_state_transition_format(self, caplog):
        """log_transition emits correctly formatted log message."""
        import logging
        caplog.set_level(logging.INFO)

        log_transition(
            entity_type="issue",
            number=42,
            from_state="AVAILABLE",
            to_state="IN_PROGRESS",
            reason="session started",
        )

        # Verify log format includes all components
        assert "[TRANSITION]" in caplog.text
        assert "issue #42" in caplog.text
        assert "AVAILABLE" in caplog.text
        assert "IN_PROGRESS" in caplog.text
        assert "session started" in caplog.text

    def test_logs_extra_details_at_debug_level(self, caplog):
        """log_transition logs extra details at DEBUG level."""
        import logging
        caplog.set_level(logging.DEBUG)

        log_transition(
            entity_type="review",
            number=100,
            from_state="QUEUED",
            to_state="STARTED",
            reason="reviewer assigned",
            extra={"reviewer": "agent:reviewer", "cycle": 1},
        )

        assert "#100" in caplog.text
        assert "extra" in caplog.text


# =============================================================================
# Tests for pause_issue_for_reconciliation
# =============================================================================


class TestPauseIssueForReconciliation:
    """Tests for pause_issue_for_reconciliation function."""

    def test_adds_pause_label_on_reconciliation_failure(
        self, mock_event_sink, mock_action_applier, sample_event_context
    ):
        """Reconciliation failure adds pause label to issue."""
        pause_issue_for_reconciliation(
            events=mock_event_sink,
            action_applier=mock_action_applier,
            event_context=sample_event_context,
            issue_number=42,
            reason="Labels changed externally",
        )

        # Should add the pause label
        pause_label = get_pause_label()
        mock_action_applier.apply.assert_called_once()
        action = mock_action_applier.apply.call_args[0][0]
        assert isinstance(action, AddLabelAction)
        assert action.issue_number == 42
        assert action.label == pause_label

    def test_emits_issue_paused_event(
        self, mock_event_sink, mock_action_applier, sample_event_context
    ):
        """Reconciliation pause emits ISSUE_PAUSED_RECONCILE event."""
        pause_issue_for_reconciliation(
            events=mock_event_sink,
            action_applier=mock_action_applier,
            event_context=sample_event_context,
            issue_number=99,
            reason="State drift detected",
        )

        # Should publish event
        mock_event_sink.publish.assert_called_once()
        event = mock_event_sink.events[0]
        assert event.name == EventName.ISSUE_PAUSED_RECONCILE
        assert event.data["issue_number"] == 99
        assert "State drift" in event.data["reason"]

    def test_handles_add_label_failure_gracefully(
        self, mock_event_sink, mock_action_applier, sample_event_context, caplog
    ):
        """If add_label fails, function logs error but doesn't raise."""
        import logging
        caplog.set_level(logging.ERROR)

        mock_action_applier.apply.side_effect = Exception("API error")

        # Should not raise
        pause_issue_for_reconciliation(
            events=mock_event_sink,
            action_applier=mock_action_applier,
            event_context=sample_event_context,
            issue_number=42,
            reason="Test failure",
        )

        # Should log error
        assert "Failed to add pause label" in caplog.text


# =============================================================================
# Tests for clear_discovered_facts
# =============================================================================


class TestClearDiscoveredFacts:
    """Tests for clear_discovered_facts function."""

    def test_clears_all_discovered_fact_lists(self, sample_orchestrator_state):
        """clear_discovered_facts empties all discovered* lists."""
        # Populate with test data
        sample_orchestrator_state.discovered_reviews = [
            DiscoveredReview(issue_number=1, pr_number=100, pr_url="url", branch_name="branch")
        ]
        sample_orchestrator_state.discovered_awaiting_merge_reconciliations = [
            DiscoveredAwaitingMergeReconciliation(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                status="merged",
                status_reason="PR merged; awaiting merge reconciled",
                source="pull_request",
            )
        ]
        sample_orchestrator_state.discovered_awaiting_merge_drifts = [
            DiscoveredAwaitingMergeDrift(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                status_reason="PR closed; issue remains open",
            )
        ]
        sample_orchestrator_state.discovered_awaiting_merge_escalations = [
            DiscoveredAwaitingMergeEscalation(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                issue_key="M0-001",
                rework_cycle=1,
                kind="branch_protection_blocked",
                reason="Branch protection blocks merge.",
            )
        ]
        sample_orchestrator_state.discovered_merge_queue_enqueues = [
            DiscoveredMergeQueueEnqueue(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                issue_key="M0-001",
            )
        ]
        sample_orchestrator_state.discovered_reworks = [
            DiscoveredRework(issue_number=2, pr_number=200, branch_name="br", agent_type="agent:dev", rework_cycle=1)
        ]
        sample_orchestrator_state.discovered_escalations = [
            DiscoveredEscalation(issue_number=3, pr_number=300, rework_cycle=3)
        ]
        sample_orchestrator_state.discovered_failures = [
            DiscoveredFailure(issue_number=4, issue_title="Test", failure_reason="failed")
        ]

        clear_discovered_facts(sample_orchestrator_state)

        # All lists should be empty
        assert len(sample_orchestrator_state.discovered_reviews) == 0
        assert len(sample_orchestrator_state.discovered_awaiting_merge_reconciliations) == 0
        assert len(sample_orchestrator_state.discovered_awaiting_merge_drifts) == 0
        assert len(sample_orchestrator_state.discovered_awaiting_merge_escalations) == 0
        assert len(sample_orchestrator_state.discovered_merge_queue_enqueues) == 0
        assert len(sample_orchestrator_state.discovered_reworks) == 0
        assert len(sample_orchestrator_state.discovered_escalations) == 0
        assert len(sample_orchestrator_state.discovered_failures) == 0

    def test_clears_immediate_cleanups(self, sample_orchestrator_state):
        """clear_discovered_facts also clears immediate_cleanups.

        This regression test ensures immediate_cleanups is cleared to prevent
        infinite cleanup loops where the same completed sessions are processed
        repeatedly.
        """
        # Add immediate cleanup entries
        sample_orchestrator_state.immediate_cleanups = [
            ImmediateCleanup(
                issue_number=1,
                terminal_id="issue-1",
                worktree_path="/tmp/worktree-1",
                reason="completed"
            ),
            ImmediateCleanup(
                issue_number=2,
                terminal_id="review-2",
                worktree_path="/tmp/worktree-2",
                reason="failed"
            ),
        ]

        clear_discovered_facts(sample_orchestrator_state)

        assert len(sample_orchestrator_state.immediate_cleanups) == 0

    def test_preserves_non_discovered_state(self, sample_orchestrator_state):
        """clear_discovered_facts does not affect other state fields."""
        # Set up some non-discovered state
        sample_orchestrator_state.paused = True
        sample_orchestrator_state.issues_started_count = 5
        sample_orchestrator_state.pending_reviews = [
            PendingReview(
                issue_key=FakeIssueKey(name="1"),
                pr_number=100,
                pr_url="url",
                branch_name="branch",
                _issue_number=1,
            )
        ]

        clear_discovered_facts(sample_orchestrator_state)

        # Non-discovered fields should be untouched
        assert sample_orchestrator_state.paused is True
        assert sample_orchestrator_state.issues_started_count == 5
        assert len(sample_orchestrator_state.pending_reviews) == 1


# =============================================================================
# Tests for emit_heartbeat_if_needed
# =============================================================================


class TestEmitHeartbeatIfNeeded:
    """Tests for emit_heartbeat_if_needed function."""

    def test_emits_heartbeat_after_interval(self, mock_event_sink, sample_event_context, sample_orchestrator_state):
        """Heartbeat is emitted when interval has elapsed."""
        last_update = time.time() - 60  # 60 seconds ago

        new_timestamp = emit_heartbeat_if_needed(
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            last_ui_update=last_update,
            ui_update_interval=30,  # 30 second interval
        )

        # Should have published heartbeat
        mock_event_sink.publish.assert_called_once()
        event = mock_event_sink.events[0]
        assert event.name == EventName.ORCHESTRATOR_IDLE

        # Should return updated timestamp
        assert new_timestamp > last_update

    def test_no_heartbeat_before_interval(self, mock_event_sink, sample_event_context, sample_orchestrator_state):
        """No heartbeat when interval has not elapsed."""
        last_update = time.time() - 5  # 5 seconds ago

        new_timestamp = emit_heartbeat_if_needed(
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            last_ui_update=last_update,
            ui_update_interval=30,  # 30 second interval
        )

        # Should NOT have published heartbeat
        mock_event_sink.publish.assert_not_called()

        # Should return same timestamp
        assert new_timestamp == last_update


# =============================================================================
# Tests for check_health
# =============================================================================


class TestCheckHealth:
    """Tests for check_health function."""

    def test_returns_ok_when_healthy(self):
        """check_health returns OK decision when system is healthy."""
        health_gate = HealthGate(max_concurrent_sessions=3)

        decision = check_health(
            health_gate=health_gate,
            active_sessions_count=1,
            paused=False,
        )

        assert decision.can_proceed is True

    def test_returns_blocked_when_paused(self):
        """check_health returns blocked when orchestrator is paused."""
        health_gate = HealthGate(max_concurrent_sessions=3)

        decision = check_health(
            health_gate=health_gate,
            active_sessions_count=0,
            paused=True,
        )

        assert decision.can_proceed is False
        assert "paused" in decision.reason

    def test_returns_blocked_when_at_capacity(self):
        """check_health returns blocked when at maximum capacity."""
        health_gate = HealthGate(max_concurrent_sessions=2)

        decision = check_health(
            health_gate=health_gate,
            active_sessions_count=2,
            paused=False,
        )

        assert decision.can_proceed is False
        assert "at_capacity" in decision.reason


# =============================================================================
# Tests for OrchestratorSupport.apply_plan
# =============================================================================


class TestOrchestratorSupportApplyPlan:
    """Tests for OrchestratorSupport.apply_plan method."""

    @pytest.fixture
    def support(
        self,
        sample_orchestrator_state,
        mock_event_sink,
        mock_repository_host,
        sample_event_context,
    ):
        """Create an OrchestratorSupport instance for testing."""
        mock_config = MagicMock()
        mock_config.cleanup = MagicMock()
        mock_config.cleanup.without_triage = MagicMock()
        mock_config.cleanup.without_triage.close_ai_session_tabs = False
        mock_config.code_review_agent = None

        mock_session_manager = MagicMock()
        mock_action_applier = MagicMock()
        mock_fact_gatherer = MagicMock()
        mock_planner = MagicMock()
        mock_worktree_manager = MagicMock()
        mock_state_machine_manager = MagicMock()
        mock_cleanup_manager = MagicMock()
        mock_cleanup_manager.should_retry_triage_issue = Mock(return_value=True)

        return OrchestratorSupport(
            config=mock_config,
            events=mock_event_sink,
            repository_host=mock_repository_host,
            state=sample_orchestrator_state,
            event_context=sample_event_context,
            session_manager=mock_session_manager,
            action_applier=mock_action_applier,
            fact_gatherer=mock_fact_gatherer,
            planner=mock_planner,
            worktree_manager=mock_worktree_manager,
            state_machine_manager=mock_state_machine_manager,
            cleanup_manager=mock_cleanup_manager,
            get_review_machine=Mock(),
            kill_session=Mock(),
        )

    def test_empty_plan_does_nothing(self, support, mock_event_sink):
        """Empty plan (action_count=0) does not emit events or apply actions."""
        empty_plan = MagicMock()
        empty_plan.action_count = 0
        empty_plan.actions = []

        pause_callback = Mock()
        support.apply_plan(empty_plan, pause_callback)

        # No events should be published for empty plan
        assert mock_event_sink.publish.call_count == 0
        pause_callback.assert_not_called()

    def test_emits_apply_started_and_completed_events(self, support, mock_event_sink):
        """Applying a plan emits APPLY_STARTED and APPLY_COMPLETED events."""
        mock_action = MagicMock()
        mock_action.action_type = ActionType.ADD_LABEL

        plan = MagicMock()
        plan.action_count = 1
        plan.actions = [mock_action]

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.details = {}
        support.action_applier.apply = Mock(return_value=mock_result)

        support.apply_plan(plan, Mock())

        # Should have APPLY_STARTED and APPLY_COMPLETED
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.APPLY_STARTED in event_names
        assert EventName.APPLY_COMPLETED in event_names

    def test_stops_applying_when_paused(self, support, sample_orchestrator_state):
        """Plan application stops when orchestrator becomes paused."""
        actions = [MagicMock(action_type=ActionType.ADD_LABEL) for _ in range(3)]
        plan = MagicMock()
        plan.action_count = 3
        plan.actions = actions

        # Pause after first action
        apply_count = 0
        def pause_on_second_apply(action):
            nonlocal apply_count
            apply_count += 1
            if apply_count == 1:
                sample_orchestrator_state.paused = True
            result = MagicMock()
            result.success = True
            result.details = {}
            return result

        support.action_applier.apply = Mock(side_effect=pause_on_second_apply)

        support.apply_plan(plan, Mock())

        # Should have stopped after 1 action (paused before second)
        assert apply_count == 1

    def test_reconciliation_required_pauses_issue(self, support, mock_event_sink):
        """ReconciliationRequired exception triggers issue pause callback."""
        mock_action = MagicMock()
        mock_action.action_type = ActionType.ADD_LABEL

        plan = MagicMock()
        plan.action_count = 1
        plan.actions = [mock_action]

        # Simulate reconciliation failure
        support.action_applier.apply = Mock(
            side_effect=ReconciliationRequired(
                entity_type="issue",
                entity_id=42,
                expected=ExternalSnapshot.for_issue(42, {"in-progress"}),
                actual=ExternalSnapshot.for_issue(42, {"blocked"}),
                reason="Labels changed",
            )
        )

        pause_callback = Mock()
        support.apply_plan(plan, pause_callback)

        # Should have called pause callback
        pause_callback.assert_called_once_with(42, "Labels changed")

        # Should have emitted RECONCILIATION_REQUIRED event
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.RECONCILIATION_REQUIRED in event_names

    def test_triage_issue_skipped_on_cooldown(self, support, mock_event_sink):
        """CREATE_TRIAGE_ISSUE action is skipped when on cooldown."""
        mock_action = MagicMock()
        mock_action.action_type = ActionType.CREATE_TRIAGE_ISSUE

        plan = MagicMock()
        plan.action_count = 1
        plan.actions = [mock_action]

        # Set cooldown active
        support.cleanup_manager.should_retry_triage_issue = Mock(return_value=False)

        support.apply_plan(plan, Mock())

        # Should not have called apply for this action
        support.action_applier.apply.assert_not_called()

        # Should have emitted APPLY_FAILED
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.APPLY_FAILED in event_names

    def test_failed_label_action_marks_failed_this_cycle(self, support, sample_orchestrator_state):
        """Label mutation failures mark the issue failed_this_cycle."""
        action = AddLabelAction(issue_number=42, label="in-progress")
        plan = MagicMock()
        plan.action_count = 1
        plan.actions = [action]

        support.action_applier.apply = Mock(
            return_value=ActionResult.fail(action, "boom")
        )

        support.apply_plan(plan, Mock())

        assert 42 in sample_orchestrator_state.failed_this_cycle

    def test_failed_launch_action_marks_failed_this_cycle(self, support, sample_orchestrator_state):
        """Launch failures must mark the issue failed_this_cycle for queue diagnostics."""
        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=42,
            command="",
            working_dir="",
        )
        plan = MagicMock()
        plan.action_count = 1
        plan.actions = [action]

        support.action_applier.apply = Mock(
            return_value=ActionResult.fail(action, "worktree create failed")
        )

        support.apply_plan(plan, Mock())

        assert 42 in sample_orchestrator_state.failed_this_cycle


# =============================================================================
# Tests for OrchestratorSupport.clear_discovered_facts method
# =============================================================================


class TestOrchestratorSupportClearDiscoveredFacts:
    """Tests for OrchestratorSupport.clear_discovered_facts method.

    This tests the instance method, not the module-level function.
    Critical for preventing infinite cleanup loops (bug fix regression test).
    """

    @pytest.fixture
    def support(
        self,
        sample_orchestrator_state,
        mock_event_sink,
        mock_repository_host,
        sample_event_context,
    ):
        """Create an OrchestratorSupport instance for testing."""
        mock_config = MagicMock()
        mock_config.cleanup = MagicMock()
        mock_config.cleanup.without_triage = MagicMock()
        mock_config.cleanup.without_triage.close_ai_session_tabs = False
        mock_config.code_review_agent = None

        return OrchestratorSupport(
            config=mock_config,
            events=mock_event_sink,
            repository_host=mock_repository_host,
            state=sample_orchestrator_state,
            event_context=sample_event_context,
            session_manager=MagicMock(),
            action_applier=MagicMock(),
            fact_gatherer=MagicMock(),
            planner=MagicMock(),
            worktree_manager=MagicMock(),
            state_machine_manager=MagicMock(),
            cleanup_manager=MagicMock(),
            get_review_machine=Mock(),
            kill_session=Mock(),
        )

    def test_clears_immediate_cleanups_via_method(self, support, sample_orchestrator_state):
        """OrchestratorSupport.clear_discovered_facts() clears immediate_cleanups.

        This is the method called by Orchestrator._clear_discovered_facts().
        Regression test for infinite cleanup loop bug.
        """
        # Populate immediate_cleanups
        sample_orchestrator_state.immediate_cleanups = [
            ImmediateCleanup(
                issue_number=1,
                terminal_id="issue-1",
                worktree_path="/tmp/wt-1",
                reason="completed"
            ),
        ]

        # Call the instance method (not the module-level function)
        support.clear_discovered_facts()

        assert len(sample_orchestrator_state.immediate_cleanups) == 0

    def test_clears_all_discovered_lists_via_method(self, support, sample_orchestrator_state):
        """OrchestratorSupport.clear_discovered_facts() clears all discovery lists."""
        # Populate all discoverable state
        sample_orchestrator_state.discovered_reviews = [
            DiscoveredReview(issue_number=1, pr_number=100, pr_url="url", branch_name="br")
        ]
        sample_orchestrator_state.discovered_awaiting_merge_reconciliations = [
            DiscoveredAwaitingMergeReconciliation(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                status="closed",
                status_reason="PR closed; awaiting merge reconciled",
                source="pull_request",
            )
        ]
        sample_orchestrator_state.discovered_awaiting_merge_drifts = [
            DiscoveredAwaitingMergeDrift(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                status_reason="PR closed; issue remains open",
            )
        ]
        sample_orchestrator_state.discovered_awaiting_merge_escalations = [
            DiscoveredAwaitingMergeEscalation(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                issue_key="M0-001",
                rework_cycle=1,
                kind="branch_protection_blocked",
                reason="Branch protection blocks merge.",
            )
        ]
        sample_orchestrator_state.discovered_merge_queue_enqueues = [
            DiscoveredMergeQueueEnqueue(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                issue_key="M0-001",
            )
        ]
        sample_orchestrator_state.discovered_reworks = [
            DiscoveredRework(issue_number=2, pr_number=200, branch_name="br", agent_type="a", rework_cycle=1)
        ]
        sample_orchestrator_state.discovered_escalations = [
            DiscoveredEscalation(issue_number=3, pr_number=300, rework_cycle=3)
        ]
        sample_orchestrator_state.discovered_failures = [
            DiscoveredFailure(issue_number=4, issue_title="T", failure_reason="f")
        ]
        sample_orchestrator_state.immediate_cleanups = [
            ImmediateCleanup(issue_number=5, terminal_id="t", worktree_path="p", reason="r")
        ]

        support.clear_discovered_facts()

        assert len(sample_orchestrator_state.discovered_reviews) == 0
        assert len(sample_orchestrator_state.discovered_awaiting_merge_reconciliations) == 0
        assert len(sample_orchestrator_state.discovered_awaiting_merge_drifts) == 0
        assert len(sample_orchestrator_state.discovered_awaiting_merge_escalations) == 0
        assert len(sample_orchestrator_state.discovered_merge_queue_enqueues) == 0
        assert len(sample_orchestrator_state.discovered_reworks) == 0
        assert len(sample_orchestrator_state.discovered_escalations) == 0
        assert len(sample_orchestrator_state.discovered_failures) == 0
        assert len(sample_orchestrator_state.immediate_cleanups) == 0


# =============================================================================
# Tests for OrchestratorSupport._update_state_after_action
# =============================================================================


class TestUpdateStateAfterAction:
    """Tests for OrchestratorSupport._update_state_after_action method."""

    @pytest.fixture
    def support_with_state(
        self,
        sample_orchestrator_state,
        mock_event_sink,
        mock_repository_host,
        sample_event_context,
    ):
        """Create support with accessible state."""
        mock_config = MagicMock()
        mock_config.cleanup = MagicMock()
        mock_config.cleanup.without_triage = MagicMock()
        mock_config.cleanup.without_triage.close_ai_session_tabs = False
        mock_config.code_review_agent = None

        return OrchestratorSupport(
            config=mock_config,
            events=mock_event_sink,
            repository_host=mock_repository_host,
            state=sample_orchestrator_state,
            event_context=sample_event_context,
            session_manager=MagicMock(),
            action_applier=MagicMock(),
            fact_gatherer=MagicMock(),
            planner=MagicMock(),
            worktree_manager=MagicMock(),
            state_machine_manager=MagicMock(),
            cleanup_manager=MagicMock(),
            get_review_machine=Mock(),
            kill_session=Mock(),
        )

    def test_queue_review_adds_to_pending_reviews(self, support_with_state, mock_repository_host):
        """QUEUE_REVIEW action adds PendingReview to state."""
        from issue_orchestrator.control.actions import QueueReviewAction

        action = QueueReviewAction(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="issue-42",
            issue_labels=("agent:web", "verbose"),
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing state mutation behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        # Should have added to pending_reviews
        assert len(support_with_state.state.pending_reviews) == 1
        review = support_with_state.state.pending_reviews[0]
        assert review.pr_number == 100
        assert review.branch_name == "issue-42"
        assert review.issue_labels == ("agent:web", "verbose")

    def test_queue_review_skips_duplicate(self, support_with_state, mock_repository_host):
        """QUEUE_REVIEW action skips if PR already in pending_reviews."""
        from issue_orchestrator.control.actions import QueueReviewAction

        # Pre-populate with existing review
        existing = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="issue-42",
            _issue_number=42,
        )
        support_with_state.state.pending_reviews.append(existing)

        action = QueueReviewAction(
            issue_number=42,
            pr_number=100,  # Same PR
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="issue-42",
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing duplicate detection behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        # Should NOT have added duplicate
        assert len(support_with_state.state.pending_reviews) == 1

    def test_queue_retrospective_review_adds_to_pending_reviews(
        self,
        support_with_state,
    ):
        """QUEUE_RETROSPECTIVE_REVIEW action adds PendingRetrospectiveReview to state."""
        from issue_orchestrator.control.actions import QueueRetrospectiveReviewAction

        action = QueueRetrospectiveReviewAction(
            issue_number=42,
            issue_title="Review existing work",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            issue_key="42",
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing state mutation behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        assert len(support_with_state.state.pending_retrospective_reviews) == 1
        review = support_with_state.state.pending_retrospective_reviews[0]
        assert review.issue_number == 42
        assert review.agent_label == "agent:web"
        assert review.trigger_label == "lack-of-review-redo"

    def test_queue_retrospective_review_skips_issue_already_in_flight(
        self,
        support_with_state,
    ):
        """QUEUE_RETROSPECTIVE_REVIEW uses the centralized in-flight guard."""
        from issue_orchestrator.control.actions import QueueRetrospectiveReviewAction

        issue = Issue(number=42, title="Active review", labels=["agent:web"])
        support_with_state.state.active_sessions.append(
            Session(
                key=SessionKey(
                    issue=FakeIssueKey("42"),
                    task=TaskKind.RETROSPECTIVE_REVIEW,
                ),
                issue=issue,
                agent_config=AgentConfig(prompt_path=Path("/tmp/prompt.md")),
                terminal_id="retrospective-review-42",
                worktree_path=Path("/tmp/work42"),
                branch_name="issue-42",
                run_assets=make_session_run_assets(
                    Path("/tmp/work42"),
                    session_name="retrospective-review-42",
                ),
            )
        )
        action = QueueRetrospectiveReviewAction(
            issue_number=42,
            issue_title="Review existing work",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            issue_key="42",
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing duplicate detection behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        assert support_with_state.state.pending_retrospective_reviews == []

    def test_queue_retrospective_review_promotes_discovered_fact(
        self,
        support_with_state,
    ):
        """Discovered retrospective review facts are the source, not a duplicate."""
        from issue_orchestrator.control.actions import QueueRetrospectiveReviewAction

        support_with_state.state.discovered_retrospective_reviews.append(
            DiscoveredRetrospectiveReview(
                issue_number=42,
                issue_title="Review existing work",
                agent_label="agent:web",
                trigger_label="lack-of-review-redo",
                issue_key="42",
            )
        )
        action = QueueRetrospectiveReviewAction(
            issue_number=42,
            issue_title="Review existing work",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            issue_key="42",
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing discovered fact to pending queue transition.
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        assert len(support_with_state.state.pending_retrospective_reviews) == 1
        assert (
            support_with_state.state.pending_retrospective_reviews[0].issue_number
            == 42
        )

    def test_queue_rework_adds_to_pending_reworks(self, support_with_state, mock_repository_host):
        """QUEUE_REWORK action adds PendingRework to state."""
        from issue_orchestrator.control.actions import QueueReworkAction

        # Add discovered rework for agent type lookup
        support_with_state.state.discovered_reworks = [
            DiscoveredRework(
                issue_number=42,
                pr_number=100,
                branch_name="issue-42",
                agent_type="agent:developer",
                rework_cycle=2,
            )
        ]

        action = QueueReworkAction(
            issue_number=42,
            rework_cycle=2,
            source="post_publish_validation",
            feedback="POST-PUBLISH VALIDATION FAILURE (address these issues):\n\nResolve merge conflicts.",
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing state mutation behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        # Should have added to pending_reworks
        assert len(support_with_state.state.pending_reworks) == 1
        rework = support_with_state.state.pending_reworks[0]
        assert rework.agent_type == "agent:developer"
        assert rework.rework_cycle == 2
        assert rework.source == "post_publish_validation"
        assert "POST-PUBLISH VALIDATION FAILURE" in (rework.feedback or "")

    def test_cleanup_session_removes_from_pending_cleanups(self, support_with_state):
        """CLEANUP_SESSION action removes cleanup from pending_cleanups."""
        from issue_orchestrator.control.actions import CleanupSessionAction

        # Pre-populate with cleanup
        mock_issue = MagicMock()
        mock_issue.number = 42
        cleanup = PendingCleanup(
            issue=mock_issue,
            pr_number=100,
            pr_url="url",
            branch_name="branch",
            terminal_id="session-42",
            worktree_path=Path("/tmp/worktree"),
        )
        support_with_state.state.pending_cleanups.append(cleanup)

        action = CleanupSessionAction(
            issue_number=42,
            pr_number=100,
            terminal_id="session-42",
            worktree_path="/tmp/worktree",
            close_tabs=True,
            remove_worktrees=True,
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing state mutation behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        # Should have removed from pending_cleanups
        assert len(support_with_state.state.pending_cleanups) == 0

    def test_create_triage_issue_adds_to_pending_triage(self, support_with_state):
        """CREATE_TRIAGE_ISSUE action adds to pending_triage_reviews."""
        from issue_orchestrator.control.actions import CreateTriageIssueAction

        action = CreateTriageIssueAction(
            title="Triage Batch Review",
            body="Review these PRs",
            labels=("agent:triage",),
            pr_count=5,
        )
        result = MagicMock(success=True, details={"issue_number": 999})

        # noqa: SLF001 - Testing state mutation behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        # Should have added to pending_triage_reviews
        assert len(support_with_state.state.pending_triage_reviews) == 1
        triage = support_with_state.state.pending_triage_reviews[0]
        assert triage.issue_number == 999


# =============================================================================
# Tests for run_tick
# =============================================================================


class TestRunTick:
    """Tests for run_tick function."""

    def test_increments_loop_iteration(self, sample_orchestrator_state, mock_event_sink, sample_event_context):
        """run_tick increments loop iteration counter."""
        inflight = {}

        new_iteration, should_continue = run_tick(
            loop_iteration=5,
            event_context=sample_event_context,
            inflight_stable_ids=inflight,
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.ok()),
            run_planning_cycle_fn=Mock(),
            emit_heartbeat_fn=Mock(),
        )

        assert new_iteration == 6

    def test_returns_false_when_shutdown_requested(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        """run_tick returns should_continue=False when shutdown requested."""
        inflight = {}

        _, should_continue = run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids=inflight,
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=True,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.ok()),
            run_planning_cycle_fn=Mock(),
            emit_heartbeat_fn=Mock(),
        )

        assert should_continue is False

    def test_emits_tick_started_and_completed_events(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        """run_tick emits TICK_STARTED and TICK_COMPLETED events."""
        inflight = {}

        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids=inflight,
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.ok()),
            run_planning_cycle_fn=Mock(),
            emit_heartbeat_fn=Mock(),
        )

        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.TICK_STARTED in event_names
        assert EventName.TICK_COMPLETED in event_names

    def test_emits_tick_slow_with_phase_breakdown_when_tick_overruns(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context, monkeypatch
    ):
        """A tick that overruns the heartbeat budget emits a machine TICK_SLOW
        event carrying the sub-phase breakdown (so the UI can attribute the
        stall instead of only inferring it from heartbeat age)."""
        import types

        clock = {"mono": 1000.0}
        fake_time = types.SimpleNamespace(
            monotonic=lambda: clock["mono"],
            time=lambda: 1_700_000_000.0,
        )
        monkeypatch.setattr(
            "issue_orchestrator.control.orchestrator_support.time", fake_time
        )

        def slow_active() -> None:
            clock["mono"] += 153.9  # the synchronous publish that froze the tick

        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids={},
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=slow_active,
            check_health_fn=Mock(return_value=HealthDecision.ok()),
            run_planning_cycle_fn=Mock(),
            emit_heartbeat_fn=Mock(),
        )

        slow_events = [e for e in mock_event_sink.events if e.name == EventName.TICK_SLOW]
        assert len(slow_events) == 1
        payload = slow_events[0].data
        assert payload["duration_seconds"] == 153.9
        assert payload["active_seconds"] == 153.9
        assert payload["dominant_phase"] == "active_sessions"

    def test_no_tick_slow_event_for_fast_tick(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        """A normal (fast) tick must not emit TICK_SLOW."""
        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids={},
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.ok()),
            run_planning_cycle_fn=Mock(),
            emit_heartbeat_fn=Mock(),
        )

        assert not [e for e in mock_event_sink.events if e.name == EventName.TICK_SLOW]

    def test_skips_planning_when_health_check_fails(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        """run_tick skips planning when health gate blocks."""
        inflight = {}
        planning_fn = Mock()

        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids=inflight,
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.blocked("at_capacity")),
            run_planning_cycle_fn=planning_fn,
            emit_heartbeat_fn=Mock(),
        )

        # Planning should NOT have been called
        planning_fn.assert_not_called()

        # Should emit PLAN_NOOP event
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.PLAN_NOOP in event_names

    def test_manual_refresh_runs_while_paused(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        """run_tick allows a pending queue refresh while keeping paused planning safe."""
        inflight = {}
        sample_orchestrator_state.paused = True
        sample_orchestrator_state.queue_refresh_requested = True
        planning_fn = Mock()

        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids=inflight,
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.blocked("paused")),
            run_planning_cycle_fn=planning_fn,
            emit_heartbeat_fn=Mock(),
        )

        planning_fn.assert_called_once()
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.PLAN_NOOP not in event_names

    def test_prunes_expired_inflight_ids(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        """run_tick removes expired inflight stable IDs."""
        now = time.monotonic()
        inflight = {
            "expired-id": now - 10,  # Already expired
            "valid-id": now + 100,   # Still valid
        }

        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids=inflight,
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.ok()),
            run_planning_cycle_fn=Mock(),
            emit_heartbeat_fn=Mock(),
        )

        # Expired ID should be removed
        assert "expired-id" not in inflight
        # Valid ID should remain
        assert "valid-id" in inflight


# =============================================================================
# Tests for OrchestratorSupport.request_refresh
# =============================================================================


class TestRequestRefresh:
    """Tests for OrchestratorSupport.request_refresh method."""

    @pytest.fixture
    def support(self, sample_orchestrator_state, mock_event_sink, mock_repository_host, sample_event_context):
        """Create a minimal OrchestratorSupport for testing."""
        mock_config = MagicMock()
        mock_config.cleanup = MagicMock()

        return OrchestratorSupport(
            config=mock_config,
            events=mock_event_sink,
            repository_host=mock_repository_host,
            state=sample_orchestrator_state,
            event_context=sample_event_context,
            session_manager=MagicMock(),
            action_applier=MagicMock(),
            fact_gatherer=MagicMock(),
            planner=MagicMock(),
            worktree_manager=MagicMock(),
            state_machine_manager=MagicMock(),
            cleanup_manager=MagicMock(),
            get_review_machine=Mock(),
            kill_session=Mock(),
        )

    def test_adds_inflight_ids_with_expiry(self, support):
        """request_refresh adds inflight IDs with expiry timestamp."""
        inflight_dict = {}
        ttl = 60.0

        support.request_refresh(
            inflight_stable_ids={"id-1", "id-2"},
            inflight_dict=inflight_dict,
            ttl=ttl,
        )

        # Both IDs should be in dict
        assert "id-1" in inflight_dict
        assert "id-2" in inflight_dict

        # Expiry should be in the future
        now = time.monotonic()
        assert inflight_dict["id-1"] > now
        assert inflight_dict["id-2"] > now

    def test_handles_empty_inflight_ids(self, support, caplog):
        """request_refresh handles None/empty inflight IDs gracefully."""
        import logging
        caplog.set_level(logging.INFO)

        inflight_dict = {}

        support.request_refresh(
            inflight_stable_ids=None,
            inflight_dict=inflight_dict,
            ttl=60.0,
        )

        # Dict should remain empty
        assert len(inflight_dict) == 0
        # Should log the refresh
        assert "[REFRESH]" in caplog.text


# =============================================================================
# Tests for OrchestratorSupport._check_health
# =============================================================================


# =============================================================================
# Tests for OrchestratorSupport._immediate_cleanup
# =============================================================================


class TestImmediateCleanup:
    """Tests for OrchestratorSupport._immediate_cleanup method."""

    @pytest.fixture
    def support_for_cleanup(
        self,
        sample_orchestrator_state,
        mock_event_sink,
        mock_repository_host,
        sample_event_context,
    ):
        """Create support for cleanup testing."""
        mock_config = MagicMock()
        mock_config.cleanup = MagicMock()
        mock_config.cleanup.without_triage = MagicMock()
        mock_config.cleanup.without_triage.close_ai_session_tabs = True
        mock_config.code_review_agent = None

        mock_worktree_manager = MagicMock()
        kill_session = Mock()

        support = OrchestratorSupport(
            config=mock_config,
            events=mock_event_sink,
            repository_host=mock_repository_host,
            state=sample_orchestrator_state,
            event_context=sample_event_context,
            session_manager=MagicMock(),
            action_applier=MagicMock(),
            fact_gatherer=MagicMock(),
            planner=MagicMock(),
            worktree_manager=mock_worktree_manager,
            state_machine_manager=MagicMock(),
            cleanup_manager=MagicMock(),
            get_review_machine=Mock(),
            kill_session=kill_session,
        )
        return support

    def test_removes_worktree_on_completed_status(self, support_for_cleanup, tmp_path):
        """COMPLETED status with close_ai_session_tabs removes worktree."""
        issue = make_issue(1)
        session = make_session(issue, tmp_path=tmp_path)

        # noqa: SLF001 - Testing cleanup behavior of private method
        support_for_cleanup._immediate_cleanup(session, SessionStatus.COMPLETED)  # noqa: SLF001

        # Should have called worktree remove
        support_for_cleanup.worktree_manager.remove.assert_called_once()

    def test_kills_session_terminal(self, support_for_cleanup, tmp_path):
        """_immediate_cleanup always attempts to kill session terminal."""
        issue = make_issue(1)
        session = make_session(issue, tmp_path=tmp_path)

        # noqa: SLF001 - Testing terminal kill behavior of private method
        support_for_cleanup._immediate_cleanup(session, SessionStatus.COMPLETED)  # noqa: SLF001

        # Should have called kill_session
        support_for_cleanup.kill_session.assert_called_once_with(session.terminal_id)

    def test_handles_cleanup_errors_gracefully(self, support_for_cleanup, tmp_path):
        """_immediate_cleanup handles errors without raising."""
        issue = make_issue(1)
        session = make_session(issue, tmp_path=tmp_path)

        # Make both operations fail
        support_for_cleanup.worktree_manager.remove.side_effect = Exception("Worktree error")
        support_for_cleanup.kill_session.side_effect = Exception("Kill error")

        # noqa: SLF001 - Testing error handling behavior of private method
        # Should not raise
        support_for_cleanup._immediate_cleanup(session, SessionStatus.COMPLETED)  # noqa: SLF001


# =============================================================================
# Tests for _track_stale_ticks
# =============================================================================


class TestTrackStaleTicks:
    """Tests for _track_stale_ticks function."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock Config with stale_escalation_ticks."""
        config = MagicMock()
        config.stale_escalation_ticks = 3  # Default threshold
        return config

    def test_increments_tick_count_for_stale_issues(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """_track_stale_ticks increments counter for stale issues."""
        stale_issues = [make_issue(42), make_issue(43)]

        _track_stale_ticks(
            config=mock_config,
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            stale_issues=stale_issues,
        )

        # Both issues should have tick count of 1
        assert sample_orchestrator_state.stale_issue_ticks[42] == 1
        assert sample_orchestrator_state.stale_issue_ticks[43] == 1

    def test_accumulates_ticks_over_multiple_calls(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """_track_stale_ticks accumulates tick counts across calls."""
        stale_issues = [make_issue(42)]

        # Call three times
        for _ in range(3):
            _track_stale_ticks(
                config=mock_config,
                events=mock_event_sink,
                event_context=sample_event_context,
                state=sample_orchestrator_state,
                stale_issues=stale_issues,
            )

        # Tick count should be 3
        assert sample_orchestrator_state.stale_issue_ticks[42] == 3

    def test_emits_cleared_event_when_issue_leaves_stale_set(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """STALE_IN_PROGRESS_CLEARED is emitted when issue is no longer stale."""
        # Set up: issue 42 was stale for 2 ticks
        sample_orchestrator_state.stale_issue_ticks[42] = 2

        # Now call with empty stale list (issue 42 is no longer stale)
        _track_stale_ticks(
            config=mock_config,
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            stale_issues=[],  # Issue 42 is no longer stale
        )

        # Issue should be removed from tracking
        assert 42 not in sample_orchestrator_state.stale_issue_ticks

        # Should have emitted STALE_IN_PROGRESS_CLEARED event
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.STALE_IN_PROGRESS_CLEARED in event_names

        # Verify event payload
        cleared_event = next(
            e for e in mock_event_sink.events
            if e.name == EventName.STALE_IN_PROGRESS_CLEARED
        )
        assert cleared_event.data["issue_number"] == 42

    def test_emits_persistent_stale_event_when_threshold_exceeded(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """PERSISTENT_STALE_DETECTED is emitted when threshold is exceeded."""
        mock_config.stale_escalation_ticks = 3

        # Pre-populate: issue has been stale for 2 ticks already
        sample_orchestrator_state.stale_issue_ticks[42] = 2

        # This call will increment to 3, hitting the threshold
        _track_stale_ticks(
            config=mock_config,
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            stale_issues=[make_issue(42)],
        )

        # Tick count should now be 3
        assert sample_orchestrator_state.stale_issue_ticks[42] == 3

        # Should have emitted PERSISTENT_STALE_DETECTED event
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.PERSISTENT_STALE_DETECTED in event_names

        # Verify event payload
        persistent_event = next(
            e for e in mock_event_sink.events
            if e.name == EventName.PERSISTENT_STALE_DETECTED
        )
        assert persistent_event.data["issue_number"] == 42
        assert persistent_event.data["consecutive_ticks"] == 3
        assert persistent_event.data["threshold"] == 3

    def test_no_persistent_event_when_threshold_disabled(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """No PERSISTENT_STALE_DETECTED when stale_escalation_ticks is 0."""
        mock_config.stale_escalation_ticks = 0  # Disabled

        # Pre-populate: issue has been stale for many ticks
        sample_orchestrator_state.stale_issue_ticks[42] = 10

        # Call again
        _track_stale_ticks(
            config=mock_config,
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            stale_issues=[make_issue(42)],
        )

        # Should NOT emit PERSISTENT_STALE_DETECTED
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.PERSISTENT_STALE_DETECTED not in event_names

    def test_no_persistent_event_below_threshold(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """No PERSISTENT_STALE_DETECTED when below threshold."""
        mock_config.stale_escalation_ticks = 5

        # Only 2 ticks so far
        sample_orchestrator_state.stale_issue_ticks[42] = 2

        # Call again (now 3 ticks, but threshold is 5)
        _track_stale_ticks(
            config=mock_config,
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            stale_issues=[make_issue(42)],
        )

        # Should NOT emit PERSISTENT_STALE_DETECTED
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.PERSISTENT_STALE_DETECTED not in event_names

    def test_clears_multiple_issues_simultaneously(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """Multiple issues can be cleared in one call."""
        # Set up: multiple issues were stale
        sample_orchestrator_state.stale_issue_ticks[42] = 2
        sample_orchestrator_state.stale_issue_ticks[43] = 3
        sample_orchestrator_state.stale_issue_ticks[44] = 1

        # Now none are stale
        _track_stale_ticks(
            config=mock_config,
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            stale_issues=[],
        )

        # All should be cleared
        assert len(sample_orchestrator_state.stale_issue_ticks) == 0

        # Should have 3 STALE_IN_PROGRESS_CLEARED events
        cleared_events = [
            e for e in mock_event_sink.events
            if e.name == EventName.STALE_IN_PROGRESS_CLEARED
        ]
        assert len(cleared_events) == 3

        # All issue numbers should be represented
        cleared_issue_nums = {e.data["issue_number"] for e in cleared_events}
        assert cleared_issue_nums == {42, 43, 44}

    def test_mixed_scenario_some_cleared_some_escalated(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """Test mixed scenario: some issues cleared, one escalated."""
        mock_config.stale_escalation_ticks = 3

        # Set up: issue 42 is about to hit threshold, issue 43 will be cleared
        sample_orchestrator_state.stale_issue_ticks[42] = 2
        sample_orchestrator_state.stale_issue_ticks[43] = 5  # Will be cleared

        # Issue 42 still stale, issue 43 is not
        _track_stale_ticks(
            config=mock_config,
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            stale_issues=[make_issue(42)],
        )

        # Issue 42 should be at 3 ticks
        assert sample_orchestrator_state.stale_issue_ticks[42] == 3
        # Issue 43 should be cleared
        assert 43 not in sample_orchestrator_state.stale_issue_ticks

        # Should have both events
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.STALE_IN_PROGRESS_CLEARED in event_names


def test_record_issue_refreshes_updates_ui_freshness_map():
    state = OrchestratorState()

    _record_issue_refreshes(
        state=state,
        refreshed_numbers={4057, 4058},
        refreshed_at=1234.5,
    )

    assert state.issue_refresh_timestamps == {4057: 1234.5, 4058: 1234.5}
    assert state.issue_last_refreshed_at == {4057: 1234.5, 4058: 1234.5}

class TestRunTickHeartbeat:
    '''Heartbeat fields populated by run_tick must reflect tick lifecycle.'''

    def test_successful_tick_updates_completion_timestamp(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids={},
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.ok()),
            run_planning_cycle_fn=Mock(),
            emit_heartbeat_fn=Mock(),
        )

        assert sample_orchestrator_state.last_tick_started_at > 0
        assert sample_orchestrator_state.last_tick_completed_at > 0
        assert (
            sample_orchestrator_state.last_tick_completed_at
            >= sample_orchestrator_state.last_tick_started_at
        )
        # Phase cleared back to empty after a successful tick.
        assert sample_orchestrator_state.current_tick_phase == ''

    def test_tick_exception_leaves_phase_set_so_stale_reason_diagnoses_stall(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        '''A raising phase must leave last_tick_completed_at stale AND expose the
        phase on OrchestratorState. The dashboard turns that combination into
        the actionable 'Orchestrator tick stalled' banner — if we wrote a
        completion timestamp in a finally: block we would hide the outage
        from the UI.
        '''
        def boom() -> None:
            raise RuntimeError('active sessions exploded')

        sample_orchestrator_state.last_tick_completed_at = 0.0

        with pytest.raises(RuntimeError, match='active sessions exploded'):
            run_tick(
                loop_iteration=1,
                event_context=sample_event_context,
                inflight_stable_ids={},
                state=sample_orchestrator_state,
                events=mock_event_sink,
                shutdown_requested=False,
                process_active_sessions_fn=boom,
                check_health_fn=Mock(return_value=HealthDecision.ok()),
                run_planning_cycle_fn=Mock(),
                emit_heartbeat_fn=Mock(),
            )

        # Started was written before boom ran; phase is still the one that
        # raised. last_tick_completed_at was never updated, so the dashboard
        # age-based stall check will fire on the next refresh.
        assert sample_orchestrator_state.last_tick_started_at > 0
        assert sample_orchestrator_state.last_tick_completed_at == 0.0
        assert sample_orchestrator_state.current_tick_phase == 'active_sessions'
