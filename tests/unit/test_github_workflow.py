from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from issue_orchestrator.adapters.github.http_client import GitHubHttpError
from issue_orchestrator.control.github_workflow import GitHubWorkflow
from issue_orchestrator.control.awaiting_merge_reconciler import AwaitingMergeReconciliationResult
from issue_orchestrator.domain.models import DiscoveredRework, OrchestratorState
from issue_orchestrator.events import EventContext
from issue_orchestrator.infra.config import Config


def test_scan_pending_pr_work_loads_issue_branches_once_and_reuses_map() -> None:
    pr_scanner = MagicMock()
    issue_branches = {42: "42-scratch-1774101016"}
    pr_scanner.load_issue_branches.return_value = issue_branches
    pr_scanner.scan_for_reviews.return_value = []
    pr_scanner.scan_for_reworks.return_value = ([], [])

    workflow = GitHubWorkflow(
        config=Config(),
        events=MagicMock(),
        repository_host=MagicMock(),
        fact_gatherer=MagicMock(),
        pr_scanner=pr_scanner,
        label_sync=None,
        event_context=EventContext(),
    )
    state = OrchestratorState()

    workflow.scan_pending_pr_work(state)

    pr_scanner.load_issue_branches.assert_called_once_with()
    pr_scanner.scan_for_reviews.assert_called_once_with(
        [],
        [],
        issue_branches=issue_branches,
    )
    pr_scanner.scan_for_reworks.assert_called_once_with(
        [],
        [],
        issue_branches=issue_branches,
    )


def test_scan_pending_pr_work_appends_post_publish_validation_reworks() -> None:
    pr_scanner = MagicMock()
    pr_scanner.load_issue_branches.return_value = {}
    pr_scanner.scan_for_reviews.return_value = []
    pr_scanner.scan_for_reworks.return_value = ([], [])

    workflow = GitHubWorkflow(
        config=Config(),
        events=MagicMock(),
        repository_host=MagicMock(),
        fact_gatherer=MagicMock(),
        pr_scanner=pr_scanner,
        label_sync=None,
        event_context=EventContext(),
    )
    state = OrchestratorState()
    discovered = DiscoveredRework(
        issue_number=42,
        pr_number=1042,
        branch_name="42-fix-validation",
        agent_type="agent:backend",
        rework_cycle=2,
        source="post_publish_validation",
        feedback="POST-PUBLISH VALIDATION FAILURE (address these issues):\n\n...",
    )

    with patch(
        "issue_orchestrator.control.github_workflow.AwaitingMergeReconciler.discover",
        return_value=AwaitingMergeReconciliationResult(
            checked=1,
            rework_discovered=1,
            reworks=(discovered,),
        ),
    ):
        workflow.scan_pending_pr_work(state)

    assert state.discovered_reworks == [discovered]


def test_scan_pending_pr_work_skips_general_scans_but_keeps_awaiting_merge_followups() -> None:
    pr_scanner = MagicMock()
    discovered = DiscoveredRework(
        issue_number=42,
        pr_number=1042,
        branch_name="42-fix-validation",
        agent_type="agent:backend",
        rework_cycle=2,
        source="post_publish_validation",
        feedback="POST-PUBLISH VALIDATION FAILURE (address these issues):\n\n...",
    )
    workflow = GitHubWorkflow(
        config=Config(),
        events=MagicMock(),
        repository_host=MagicMock(),
        fact_gatherer=MagicMock(),
        pr_scanner=pr_scanner,
        label_sync=None,
        event_context=EventContext(),
    )
    state = OrchestratorState()

    with patch(
        "issue_orchestrator.control.github_workflow.AwaitingMergeReconciler.discover",
        return_value=AwaitingMergeReconciliationResult(
            checked=1,
            rework_discovered=1,
            reworks=(discovered,),
        ),
    ):
        workflow.scan_pending_pr_work(state, include_general_scans=False)

    pr_scanner.load_issue_branches.assert_not_called()
    pr_scanner.scan_for_reviews.assert_not_called()
    pr_scanner.scan_for_reworks.assert_not_called()
    assert state.discovered_reworks == [discovered]


def test_fetch_delta_issues_propagates_repository_error() -> None:
    repository_host = MagicMock()
    repository_host.list_issues_delta.side_effect = GitHubHttpError(
        "GitHub unavailable",
        status_code=503,
    )
    workflow = GitHubWorkflow(
        config=Config(),
        events=MagicMock(),
        repository_host=repository_host,
        fact_gatherer=MagicMock(),
        pr_scanner=MagicMock(),
        label_sync=None,
        event_context=EventContext(),
    )

    with pytest.raises(GitHubHttpError) as exc_info:
        workflow.fetch_delta_issues(since="2026-01-01T00:00:00Z", fetch_limit=25)

    assert exc_info.value.status_code == 503


def test_refresh_issue_propagates_repository_error() -> None:
    repository_host = MagicMock()
    repository_host.get_issue.side_effect = GitHubHttpError(
        "GitHub unavailable",
        status_code=503,
    )
    workflow = GitHubWorkflow(
        config=Config(),
        events=MagicMock(),
        repository_host=repository_host,
        fact_gatherer=MagicMock(),
        pr_scanner=MagicMock(),
        label_sync=None,
        event_context=EventContext(),
    )

    with pytest.raises(GitHubHttpError) as exc_info:
        workflow.refresh_issue(42)

    assert exc_info.value.status_code == 503
