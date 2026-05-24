"""Issue action control route tests split from test_control_api."""

# ruff: noqa: F403,F405

from tests.unit import test_control_api as _support
from tests.unit.test_control_api import *  # noqa: F403

from issue_orchestrator.control.actions import (
    ActionResult as PlanActionResult,
    CloseIssueAction,
)
from issue_orchestrator.domain.models import Issue

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)

class TestResumeIssueEndpoint:
    """Test the POST /api/issues/{issue_number}/resume endpoint."""

    def test_resume_returns_503_when_orchestrator_not_initialized(
        self, client_without_orchestrator
    ):
        """Returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/issues/123/resume")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_resume_returns_404_when_worktree_not_found(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 404 when worktree does not exist."""
        client, mock_orch = client_with_orchestrator

        with patch(
            "issue_orchestrator.entrypoints.control_api_issue_routes.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = tmp_path / "nonexistent-worktree"

            response = client.post("/api/issues/123/resume")

        assert response.status_code == 404
        data = response.json()
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    def test_resume_returns_404_when_no_completion_record(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 404 when completion.json does not exist."""
        client, mock_orch = client_with_orchestrator

        # Create worktree without completion.json
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.entrypoints.control_api_issue_routes.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/resume")

        assert response.status_code == 404
        data = response.json()
        assert data["success"] is False
        assert "completion" in data["error"].lower()

    def test_resume_processes_completion_successfully(
        self, client_with_orchestrator, tmp_path
    ):
        """Successfully processes completion when worktree and completion.json exist."""
        client, mock_orch = client_with_orchestrator

        # Create worktree with completion.json
        worktree = tmp_path / "repo-123"
        worktree.mkdir()
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir()
        completion_path = completion_dir / "completion.json"
        completion_path.write_text('{"outcome": "completed"}')

        # Mock the completion processor
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.message = "Completion processed"
        mock_result.pr_url = "https://github.com/test/repo/pull/456"
        mock_result.actions_taken = ["pushed", "pr_created"]
        mock_result.errors = []
        mock_orch.deps.completion_processor.process.return_value = mock_result

        with patch(
            "issue_orchestrator.entrypoints.control_api_issue_routes.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/resume")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["message"] == "Completion processed"
        assert data["pr_url"] == "https://github.com/test/repo/pull/456"
        assert data["actions_taken"] == ["pushed", "pr_created"]

        # Verify completion processor was called with correct args
        mock_orch.deps.completion_processor.process.assert_called_once()
        call_kwargs = mock_orch.deps.completion_processor.process.call_args.kwargs
        assert call_kwargs["worktree"] == worktree
        assert call_kwargs["issue_number"] == 123

    def test_resume_uses_non_legacy_completion_path(
        self, client_with_orchestrator, tmp_path
    ):
        """Uses manifest completion_path when present."""
        client, mock_orch = client_with_orchestrator

        worktree = tmp_path / "repo-123"
        worktree.mkdir()
        run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
        run_dir.mkdir(parents=True)
        completion_path = ".issue-orchestrator/sessions/run-1/completion-issue.json"
        (worktree / completion_path).write_text('{"outcome": "completed"}')

        mock_orch.deps.session_output.find_run_dir.return_value = run_dir
        mock_orch.deps.session_output.read_manifest.return_value = {
            "completion_path": completion_path
        }

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.message = "Completion processed"
        mock_result.pr_url = None
        mock_result.actions_taken = []
        mock_result.errors = []
        mock_orch.deps.completion_processor.process.return_value = mock_result

        with patch(
            "issue_orchestrator.entrypoints.control_api_issue_routes.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/resume")

        assert response.status_code == 200
        call_kwargs = mock_orch.deps.completion_processor.process.call_args.kwargs
        assert call_kwargs["completion_path"] == completion_path

    def test_resume_handles_processing_failure(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns error when completion processing fails."""
        client, mock_orch = client_with_orchestrator

        # Create worktree with completion.json
        worktree = tmp_path / "repo-123"
        worktree.mkdir()
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir()
        completion_path = completion_dir / "completion.json"
        completion_path.write_text('{"outcome": "completed"}')

        # Mock the completion processor to raise an exception
        mock_orch.deps.completion_processor.process.side_effect = Exception(
            "Push failed: remote rejected"
        )

        with patch(
            "issue_orchestrator.entrypoints.control_api_issue_routes.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/resume")

        assert response.status_code == 500
        data = response.json()
        assert data["success"] is False
        assert "remote rejected" in data["error"]

    def test_resume_fetches_issue_title_from_cache(
        self, client_with_orchestrator, tmp_path
    ):
        """Uses cached issue title when available."""
        client, mock_orch = client_with_orchestrator

        # Create worktree with completion.json
        worktree = tmp_path / "repo-123"
        worktree.mkdir()
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir()
        (completion_dir / "completion.json").write_text('{"outcome": "completed"}')

        # Add issue to cached queue
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "Cached Issue Title"
        mock_orch.state.cached_queue_issues = [mock_issue]

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.message = "OK"
        mock_result.pr_url = None
        mock_result.actions_taken = []
        mock_result.errors = []
        mock_orch.deps.completion_processor.process.return_value = mock_result

        with patch(
            "issue_orchestrator.entrypoints.control_api_issue_routes.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/resume")

        assert response.status_code == 200
        # Verify title was used from cache
        call_kwargs = mock_orch.deps.completion_processor.process.call_args.kwargs
        assert call_kwargs["issue_title"] == "Cached Issue Title"


class TestDebugSessionEndpoint:
    """Test the POST /api/issues/{issue_number}/debug-session endpoint."""

    def test_debug_session_returns_503_when_orchestrator_not_initialized(
        self, client_without_orchestrator
    ):
        """Returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/issues/123/debug-session")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_debug_session_returns_404_when_worktree_not_found(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 404 when worktree does not exist."""
        client, mock_orch = client_with_orchestrator

        with patch(
            "issue_orchestrator.entrypoints.control_api_issue_routes.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = tmp_path / "nonexistent-worktree"

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 404
        data = response.json()
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    def test_debug_session_returns_404_when_issue_not_found(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 404 when issue is not in cache and can't be fetched."""
        client, mock_orch = client_with_orchestrator

        # Create worktree
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # Empty cached queue
        mock_orch.state.cached_queue_issues = []
        # GitHub fetch returns None
        mock_orch.deps.repository_host.get_issue.return_value = None

        with patch(
            "issue_orchestrator.entrypoints.control_api_issue_routes.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 404
        data = response.json()
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    def test_debug_session_returns_400_when_no_agent_type(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 400 when issue has no agent type label."""
        client, mock_orch = client_with_orchestrator

        # Create worktree
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # Issue without agent type
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "Test Issue"
        mock_issue.agent_type = None
        mock_orch.state.cached_queue_issues = [mock_issue]

        with patch(
            "issue_orchestrator.entrypoints.control_api_issue_routes.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 400
        data = response.json()
        assert data["success"] is False
        assert "no agent type" in data["error"].lower()

    def test_debug_session_returns_400_when_agent_config_not_found(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 400 when agent config is not found."""
        client, mock_orch = client_with_orchestrator

        # Create worktree
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # Issue with agent type but no config
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "Test Issue"
        mock_issue.agent_type = "agent:unknown"
        mock_orch.state.cached_queue_issues = [mock_issue]
        mock_orch.config.agents = {}  # No agent configs

        with patch(
            "issue_orchestrator.entrypoints.control_api_issue_routes.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 400
        data = response.json()
        assert data["success"] is False
        assert "no agent config" in data["error"].lower()

    def test_debug_session_returns_409_when_session_already_exists(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 409 when a debug session already exists for the issue."""
        client, mock_orch = client_with_orchestrator

        # Create worktree
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # Issue with agent type
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "Test Issue"
        mock_issue.agent_type = "agent:claude"
        mock_orch.state.cached_queue_issues = [mock_issue]

        # Agent config exists
        mock_agent_config = MagicMock()
        mock_agent_config.provider = None
        mock_agent_config.model = "sonnet"
        mock_orch.config.agents = {"agent:claude": mock_agent_config}

        # Session already exists
        mock_orch.deps.runner.session_exists.return_value = True

        with patch(
            "issue_orchestrator.entrypoints.control_api_issue_routes.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 409
        data = response.json()
        assert data["success"] is False
        assert "already exists" in data["error"].lower()

    def test_debug_session_launches_successfully(
        self, client_with_orchestrator, tmp_path
    ):
        """Successfully launches debug session when worktree and issue exist."""
        client, mock_orch = client_with_orchestrator

        # Create worktree
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # Issue with agent type
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "Test Issue"
        mock_issue.agent_type = "agent:claude"
        mock_orch.state.cached_queue_issues = [mock_issue]

        # Agent config - get_command returns the base command
        mock_agent_config = MagicMock()
        mock_agent_config.get_command.return_value = "claude --model sonnet 'Work on issue'"
        mock_orch.config.agents = {"agent:claude": mock_agent_config}
        mock_orch.config.web_port = 8080
        mock_orch.config.control_api_port = 8080

        # Session doesn't exist yet
        mock_orch.deps.runner.session_exists.return_value = False
        # Session creation succeeds
        mock_orch.deps.runner.create_session.return_value = True
        mock_orch.deps.session_output.ensure_run_dir.return_value = tmp_path / "run-dir"

        with patch(
            "issue_orchestrator.entrypoints.control_api_issue_routes.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["session_name"] == "debug-123"
        assert data["worktree_path"] == str(worktree)
        assert data["agent"] == "claude"
        assert "coding-done --resume" in data["hint"]

        # Verify get_command was called with debug context
        mock_agent_config.get_command.assert_called_once()
        call_kwargs = mock_agent_config.get_command.call_args.kwargs
        assert call_kwargs["issue_number"] == 123
        assert call_kwargs["issue_title"] == "Test Issue"
        assert call_kwargs["worktree"] == worktree
        assert "DEBUG SESSION" in call_kwargs["existing_work"]

        # Verify session was created with correct args
        mock_orch.deps.runner.create_session.assert_called_once()
        call_kwargs = mock_orch.deps.runner.create_session.call_args.kwargs
        assert call_kwargs["session_id"] == 123
        assert call_kwargs["working_dir"] == str(worktree)
        assert call_kwargs["session_name"] == "debug-123"
        assert "ORCHESTRATOR_ISSUE_NUMBER='123'" in call_kwargs["command"]
        assert "ORCHESTRATOR_API_PORT='8080'" in call_kwargs["command"]
        assert "ORCHESTRATOR_SESSION_ID='debug-123'" in call_kwargs["command"]
        assert "ISSUE_ORCHESTRATOR_COMPLETION_PATH='.issue-orchestrator/sessions/debug-123/completion-agent_claude.json'" in call_kwargs["command"]
        mock_orch.deps.session_output.update_manifest.assert_called_once()

    def test_debug_session_returns_500_when_session_creation_fails(
        self, client_with_orchestrator, tmp_path
    ):
        """Returns 500 when terminal session creation fails."""
        client, mock_orch = client_with_orchestrator

        # Create worktree
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # Issue with agent type
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "Test Issue"
        mock_issue.agent_type = "agent:claude"
        mock_orch.state.cached_queue_issues = [mock_issue]

        # Agent config
        mock_agent_config = MagicMock()
        mock_agent_config.get_command.return_value = "claude 'Work on issue'"
        mock_orch.config.agents = {"agent:claude": mock_agent_config}
        mock_orch.config.web_port = 8080
        mock_orch.config.control_api_port = 8080

        # Session doesn't exist yet
        mock_orch.deps.runner.session_exists.return_value = False
        # Session creation fails
        mock_orch.deps.runner.create_session.return_value = False

        with patch(
            "issue_orchestrator.entrypoints.control_api_issue_routes.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 500
        data = response.json()
        assert data["success"] is False
        assert "failed to create" in data["error"].lower()

    def test_debug_session_uses_cached_issue_over_github_fetch(
        self, client_with_orchestrator, tmp_path
    ):
        """Uses cached issue data when available."""
        client, mock_orch = client_with_orchestrator

        # Create worktree
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # Issue in cache
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "Cached Title"
        mock_issue.agent_type = "agent:claude"
        mock_orch.state.cached_queue_issues = [mock_issue]

        # Agent config
        mock_agent_config = MagicMock()
        mock_agent_config.get_command.return_value = "claude 'Work on issue'"
        mock_orch.config.agents = {"agent:claude": mock_agent_config}
        mock_orch.config.web_port = 8080
        mock_orch.config.control_api_port = 8080

        mock_orch.deps.runner.session_exists.return_value = False
        mock_orch.deps.runner.create_session.return_value = True

        with patch(
            "issue_orchestrator.entrypoints.control_api_issue_routes.get_worktree_path"
        ) as mock_get_path:
            mock_get_path.return_value = worktree

            response = client.post("/api/issues/123/debug-session")

        assert response.status_code == 200
        # GitHub should not have been called since issue was in cache
        mock_orch.deps.repository_host.get_issue.assert_not_called()


# --- Test: E2E Logs Endpoint ---



class TestRetryIssueEndpoint:
    """Test the POST /api/issues/{issue_number}/retry endpoint."""

    def test_retry_returns_503_when_orchestrator_not_initialized(
        self, client_without_orchestrator
    ):
        """Returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/issues/123/retry")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_retry_removes_blocked_labels(self, client_with_orchestrator):
        """Retry removes blocked-related labels from the issue."""
        client, mock_orch = client_with_orchestrator
        cached_issue = Issue(
            number=123,
            title="Blocked issue",
            labels=["agent:web", "blocked", "pr-pending"],
        )
        mock_orch.state.cached_scope_issues = [cached_issue]
        mock_orch.state.cached_queue_issues = [cached_issue]
        mock_orch.deps.queue_cache_store = MagicMock()

        # Mock the repository_host to track remove_label calls
        removed_labels = []

        def track_remove_label(issue_number: int, label: str):
            removed_labels.append((issue_number, label))

        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.get_issue_labels = MagicMock(
            return_value=["agent:web", "blocked", "pr-pending"]
        )
        mock_orch.repository_host.remove_label = MagicMock(
            side_effect=track_remove_label
        )

        response = client.post("/api/issues/123/retry")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "retry" in data["message"].lower()

        # Verify correct labels were targeted for removal
        removed_issue_numbers = [num for num, _ in removed_labels]
        assert all(num == 123 for num in removed_issue_numbers)
        # Should attempt to remove blocked + pr-pending (retry-gating labels)
        assert len(removed_labels) == 2
        assert (123, "blocked") in removed_labels
        assert (123, "pr-pending") in removed_labels
        assert [issue.labels for issue in mock_orch.state.cached_scope_issues] == [
            ("agent:web",)
        ]
        mock_orch.deps.queue_cache_store.save_snapshot.assert_called_once_with(
            mock_orch.state.cached_scope_issues,
            mock_orch.state.queue_delta_watermark,
            repo="test/repo",
        )

    def test_retry_handles_label_removal_failure_gracefully(
        self, client_with_orchestrator
    ):
        """Retry continues even when label removal fails (label may not exist)."""
        client, mock_orch = client_with_orchestrator

        # Mock the repository_host to raise exception on label removal
        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.get_issue_labels = MagicMock(
            return_value=["blocked", "pr-pending"]
        )
        mock_orch.repository_host.remove_label = MagicMock(
            side_effect=Exception("Label not found")
        )

        response = client.post("/api/issues/123/retry")

        # Should still succeed (silent exception handling is acceptable for missing labels)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_retry_preserves_gates_when_label_removal_partially_failed(
        self, client_with_orchestrator
    ):
        """When any retry-gating label removal fails, in-memory gates stay.

        Repro: if GitHub still has ``blocked-failed`` on the issue (because
        the remove_label() call errored) and we cleared session_history /
        failed_this_cycle anyway, the planner would re-launch into an
        issue GitHub still considers blocked. Codex review on PR #6359
        flagged this; the fix is to skip the state reset on partial
        failure and surface the partial state to logs.
        """
        from issue_orchestrator.domain.models import SessionHistoryEntry

        client, mock_orch = client_with_orchestrator

        original_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Timed out issue",
                agent_type="agent:web",
                status="timed_out",
                runtime_minutes=95,
            )
        ]
        original_failed_this_cycle = {123, 999}
        mock_orch.state.session_history = list(original_history)
        mock_orch.state.failed_this_cycle = set(original_failed_this_cycle)
        cached_issue = Issue(
            number=123,
            title="Timed out issue",
            labels=["agent:web", "blocked", "blocked-failed"],
        )
        mock_orch.state.cached_scope_issues = [cached_issue]
        mock_orch.state.cached_queue_issues = []
        mock_orch.deps.queue_cache_store = MagicMock()

        # Simulate a partial GitHub-side outage: removing `blocked` succeeds
        # but removing `blocked-failed` errors. The endpoint must NOT
        # treat the issue as fully unblocked.
        def selective_remove(_issue_number: int, label: str) -> None:
            if label == "blocked-failed":
                raise Exception("Label removal failed")

        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.get_issue_labels = MagicMock(
            return_value=["agent:web", "blocked", "blocked-failed"]
        )
        mock_orch.repository_host.remove_label = MagicMock(
            side_effect=selective_remove
        )

        response = client.post("/api/issues/123/retry")
        assert response.status_code == 200
        assert response.json()["success"] is True

        # In-memory gates left untouched — planner will keep skipping the
        # issue, which is correct because GitHub still has blocked-failed.
        assert mock_orch.state.session_history == original_history
        assert mock_orch.state.failed_this_cycle == original_failed_this_cycle
        # The queue-cache upsert is the partner side-effect of the state
        # reset; on partial failure neither runs.
        mock_orch.deps.queue_cache_store.save_snapshot.assert_not_called()

    def test_retry_prunes_session_history_and_requeues_timed_out_issue(
        self, client_with_orchestrator
    ):
        """Retry must clear session_history + failed_this_cycle and re-add
        the issue to the queue cache.

        Reproduces the real failure: a timed-out issue lives in
        `cached_scope_issues` but `evaluate_issue` rejects it from
        `cached_queue_issues` as REJECTED_EXCLUDED because its number is in
        `state.session_history`. Removing only the GitHub label leaves the
        planner skipping it on every refresh.
        """
        from issue_orchestrator.domain.models import SessionHistoryEntry

        client, mock_orch = client_with_orchestrator

        # State after a timeout: history entry present, failed_this_cycle
        # has the issue, label-side has blocked-failed, scope cache has it
        # but the queue cache does not.
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Timed out issue",
                agent_type="agent:web",
                status="timed_out",
                runtime_minutes=95,
            )
        ]
        mock_orch.state.failed_this_cycle = {123, 999}
        cached_issue = Issue(
            number=123,
            title="Timed out issue",
            labels=["agent:web", "blocked-failed"],
        )
        mock_orch.state.cached_scope_issues = [cached_issue]
        mock_orch.state.cached_queue_issues = []  # was rejected at refresh time
        mock_orch.deps.queue_cache_store = MagicMock()

        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.get_issue_labels = MagicMock(
            return_value=["agent:web", "blocked-failed"]
        )
        mock_orch.repository_host.remove_label = MagicMock()

        response = client.post("/api/issues/123/retry")

        assert response.status_code == 200
        assert response.json()["success"] is True

        # session_history entry for this issue is gone; others would survive.
        assert [e.issue_number for e in mock_orch.state.session_history] == []
        # failed_this_cycle no longer contains this issue but keeps others.
        assert mock_orch.state.failed_this_cycle == {999}
        # Re-evaluation put the issue back in the queue cache with the
        # updated label set so the next planner tick can pick it up.
        assert [i.number for i in mock_orch.state.cached_queue_issues] == [123]
        assert mock_orch.state.cached_queue_issues[0].labels == ("agent:web",)
        mock_orch.deps.queue_cache_store.save_snapshot.assert_called_once()


class TestCloseIssueEndpoint:
    """Test the POST /api/issues/{issue_number}/close endpoint."""

    def test_close_returns_503_when_orchestrator_not_initialized(
        self, client_without_orchestrator
    ):
        """Returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/issues/123/close")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_close_applies_close_action_and_prunes_cached_issue(
        self, client_with_orchestrator
    ):
        """Close delegates to ActionApplier and removes the issue from UI caches."""
        client, mock_orch = client_with_orchestrator
        cached_issue = Issue(
            number=123,
            title="Stale PR pending issue",
            labels=["agent:web", "pr-pending", "blocked:pr-closed"],
        )
        mock_orch.state.cached_scope_issues = [cached_issue]
        mock_orch.state.cached_queue_issues = [cached_issue]
        mock_orch.deps.queue_cache_store = MagicMock()
        mock_orch.deps.action_applier.apply.return_value = PlanActionResult.ok(
            CloseIssueAction(issue_number=123),
            issue_number=123,
            state="closed",
        )

        response = client.post("/api/issues/123/close")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["issue_number"] == 123
        action = mock_orch.deps.action_applier.apply.call_args.args[0]
        assert isinstance(action, CloseIssueAction)
        assert action.issue_number == 123
        assert action.expected is not None
        assert action.expected.required_labels == frozenset({"blocked:pr-closed"})
        assert mock_orch.state.cached_scope_issues == []
        assert mock_orch.state.cached_queue_issues == []
        mock_orch.deps.queue_cache_store.save_snapshot.assert_called_once_with(
            [],
            mock_orch.state.queue_delta_watermark,
            repo="test/repo",
        )

    def test_close_returns_500_when_action_fails(self, client_with_orchestrator):
        """Failed close actions are surfaced to the UI."""
        client, mock_orch = client_with_orchestrator
        mock_orch.deps.action_applier.apply.return_value = PlanActionResult.fail(
            CloseIssueAction(issue_number=123),
            "GitHub refused",
        )

        response = client.post("/api/issues/123/close")

        assert response.status_code == 500
        data = response.json()
        assert data["success"] is False
        assert data["error"] == "GitHub refused"


# --- Test: Dismiss Issue Endpoint ---


class TestDismissIssueEndpoint:
    """Test the POST /api/issues/{issue_number}/dismiss endpoint."""

    def test_dismiss_returns_503_when_orchestrator_not_initialized(
        self, client_without_orchestrator
    ):
        """Returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/issues/123/dismiss")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_dismiss_removes_labels_and_session_history(self, client_with_orchestrator):
        """Dismiss removes blocked and in-progress labels, plus session history entry."""
        client, mock_orch = client_with_orchestrator

        # Set up session history with an entry for issue 123
        from issue_orchestrator.domain.models import SessionHistoryEntry

        history_entry = SessionHistoryEntry(
            issue_number=123,
            title="Test Issue",
            agent_type="agent:claude",
            status="needs_human",
            runtime_minutes=10,
        )
        mock_orch.state.session_history = [history_entry]
        cached_issue = Issue(
            number=123,
            title="Test Issue",
            labels=["agent:web", "blocked"],
        )
        mock_orch.state.cached_scope_issues = [cached_issue]
        mock_orch.state.cached_queue_issues = [cached_issue]
        mock_orch.deps.queue_cache_store = MagicMock()

        # Mock the repository_host
        removed_labels = []

        def track_remove_label(issue_number: int, label: str):
            removed_labels.append((issue_number, label))

        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.remove_label = MagicMock(
            side_effect=track_remove_label
        )

        response = client.post("/api/issues/123/dismiss")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "dismiss" in data["message"].lower()

        # Verify labels were targeted for removal (blocked, blocked-needs-human, blocked-failed, in-progress)
        removed_issue_numbers = [num for num, _ in removed_labels]
        assert all(num == 123 for num in removed_issue_numbers)
        assert len(removed_labels) == 4  # blocked, blocked-needs-human, blocked-failed, in-progress

        # Verify session history entry was removed
        assert len(mock_orch.state.session_history) == 0
        assert mock_orch.state.cached_scope_issues == []
        assert mock_orch.state.cached_queue_issues == []
        mock_orch.deps.queue_cache_store.save_snapshot.assert_called_once_with(
            [],
            mock_orch.state.queue_delta_watermark,
            repo="test/repo",
        )

    def test_dismiss_handles_missing_session_history(self, client_with_orchestrator):
        """Dismiss succeeds even when issue not in session history."""
        client, mock_orch = client_with_orchestrator

        # Empty session history
        mock_orch.state.session_history = []
        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.remove_label = MagicMock()

        response = client.post("/api/issues/456/dismiss")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_dismiss_handles_label_removal_failure_gracefully(
        self, client_with_orchestrator
    ):
        """Dismiss continues even when label removal fails (label may not exist)."""
        client, mock_orch = client_with_orchestrator

        mock_orch.state.session_history = []
        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.remove_label = MagicMock(
            side_effect=Exception("Label not found")
        )

        response = client.post("/api/issues/123/dismiss")

        # Should still succeed (silent exception handling is acceptable for missing labels)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
