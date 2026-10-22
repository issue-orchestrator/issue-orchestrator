"""Issue action control route tests split from test_control_api."""

from tests.unit.control.liveness_doubles import liveness_owner

# ruff: noqa: F403,F405

from pathlib import Path

from tests.unit import test_control_api as _support
from tests.unit.test_control_api import *  # noqa: F403

from issue_orchestrator.control.actions import (
    ActionResult as PlanActionResult,
    CloseIssueAction,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.ports.debug_session_launch import DebugSessionOutcome, DebugSessionStatus
from tests.label_application_helpers import standing_while_present

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)


# Receipt-bound resume producer/handler cases live in test_completion_intake_routes.py.
# The previous caller-selected run-directory resume contract is intentionally gone.


def _debug_outcome(status: DebugSessionStatus, **fields) -> DebugSessionOutcome:
    return DebugSessionOutcome(
        status=status, issue_number=123, session_name="debug-123",
        worktree_path=Path("/wt/repo-123"), **fields,
    )


class TestDebugSessionEndpoint:
    """POST /api/issues/{issue_number}/debug-session asks the launch owner and maps its outcome (#9012)."""

    def test_debug_session_returns_503_when_orchestrator_not_initialized(
        self, client_without_orchestrator
    ):
        """Returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/issues/123/debug-session")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_the_route_delegates_the_launch_to_the_orchestrator(self, client_with_orchestrator):
        """The transport assembles nothing: no run, prompt, command or terminal call of its own."""
        client, mock_orch = client_with_orchestrator
        mock_orch.launch_debug_session.return_value = _debug_outcome(
            DebugSessionStatus.LAUNCHED, agent_label="agent:claude",
        )

        response = client.post("/api/issues/123/debug-session")

        mock_orch.launch_debug_session.assert_called_once_with(123)
        mock_orch.deps.runner.create_session.assert_not_called()
        mock_orch.deps.issue_run_allocator.allocate.assert_not_called()
        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "session_name": "debug-123",
            "worktree_path": "/wt/repo-123",
            "agent": "claude",
            "hint": "Debug session launched. When done, run 'coding-done --resume' to process completion.",
        }

    @pytest.mark.parametrize(("outcome", "status_code", "error"), [
        (_debug_outcome(DebugSessionStatus.WORKTREE_MISSING), 404, "Worktree not found: /wt/repo-123"),
        (_debug_outcome(DebugSessionStatus.ISSUE_NOT_FOUND), 404, "Issue #123 not found"),
        (_debug_outcome(DebugSessionStatus.NO_AGENT_LABEL), 400, "Issue has no agent type label"),
        (_debug_outcome(DebugSessionStatus.NO_AGENT_CONFIG, agent_label="agent:unknown"), 400, "No agent config for agent:unknown"),
        (_debug_outcome(DebugSessionStatus.ALREADY_RUNNING, agent_label="agent:claude"), 409, "Debug session already exists: debug-123"),
        (_debug_outcome(DebugSessionStatus.UNAVAILABLE, reason="instructions unreadable"), 503, "instructions unreadable"),
        (_debug_outcome(DebugSessionStatus.COMMAND_TOO_LONG, reason="exec argument is 200000 bytes"), 500, "exec argument is 200000 bytes"),
        (_debug_outcome(DebugSessionStatus.SPAWN_FAILED, agent_label="agent:claude"), 500, "Failed to create terminal session"),
    ], ids=lambda value: value.status.value if isinstance(value, DebugSessionOutcome) else None)
    def test_each_refusal_is_mapped_to_its_status_and_error(
        self, client_with_orchestrator, outcome, status_code, error,
    ):
        client, mock_orch = client_with_orchestrator
        mock_orch.launch_debug_session.return_value = outcome

        response = client.post("/api/issues/123/debug-session")

        assert response.status_code == status_code
        assert response.json()["success"] is False and response.json()["error"] == error

    def test_every_outcome_has_an_http_mapping(self, client_with_orchestrator):
        client, mock_orch = client_with_orchestrator
        for status in DebugSessionStatus:
            mock_orch.launch_debug_session.return_value = _debug_outcome(status, agent_label="agent:claude")

            assert client.post("/api/issues/123/debug-session").status_code in {200, 400, 404, 409, 500, 503}


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
        # No open PR: Retry clears pr-pending too (#7293 keeps it otherwise).
        mock_orch.repository_host.list_open_prs_complete.return_value = []
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

    def test_retry_reports_failure_when_no_labels_could_be_removed(
        self, client_with_orchestrator
    ):
        """If every retry-gating label fails to remove, the endpoint must
        report failure so the dashboard does not show a misleading
        "queued for retry" toast for an issue GitHub still has blocked.

        Previously this test asserted ``success: true`` on the grounds that
        \"silent exception handling is acceptable for missing labels.\" That
        was wrong: a missing label is not the same as a failed-removal —
        the wrapper exception fires for any error, including a real
        GitHub-side failure. Codex review on PR #6359 caught the wider
        version of this bug.
        """
        client, mock_orch = client_with_orchestrator

        # Mock the repository_host to raise exception on label removal
        mock_orch.repository_host = MagicMock()
        # No open PR: Retry clears pr-pending too (#7293 keeps it otherwise).
        mock_orch.repository_host.list_open_prs_complete.return_value = []
        mock_orch.repository_host.get_issue_labels = MagicMock(
            return_value=["blocked", "pr-pending"]
        )
        mock_orch.repository_host.remove_label = MagicMock(
            side_effect=Exception("Label not found")
        )

        response = client.post("/api/issues/123/retry")

        assert response.status_code == 409
        body = response.json()
        assert body["success"] is False
        assert "blocked" in body["failed_labels"]
        assert "pr-pending" in body["failed_labels"]
        assert body["removed_labels"] == []

    def test_retry_reports_partial_failure_and_preserves_gates(
        self, client_with_orchestrator
    ):
        """When any retry-gating label removal fails, in-memory gates stay
        AND the endpoint reports the partial failure so the dashboard
        does not optimistically requeue.

        Repro chain (Codex re-review on PR #6359):
        1. Endpoint removes some labels but `remove_label('blocked-failed')`
           errors → GitHub still has the gating label.
        2. If we cleared session_history / failed_this_cycle anyway, the
           planner would re-launch into an issue GitHub still considers
           blocked. (Fixed by the prior commit.)
        3. If we then returned ``success: true``, the dashboard
           (``diagnostics_actions.js::retryIssue``) trusts the flag,
           shows "Issue #N queued for retry", and applies the optimistic
           requeue — but the planner keeps skipping. The user sees the
           same "stuck" symptom this PR was meant to remove.

        So the endpoint must surface the partial failure: HTTP 409,
        ``success=False``, ``failed_labels`` listing what didn't come off,
        and ``removed_labels`` showing what did. The dashboard's else
        branch reads ``data.error`` and surfaces it as an error toast.
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
        # No open PR: Retry clears pr-pending too (#7293 keeps it otherwise).
        mock_orch.repository_host.list_open_prs_complete.return_value = []
        mock_orch.repository_host.get_issue_labels = MagicMock(
            return_value=["agent:web", "blocked", "blocked-failed"]
        )
        mock_orch.repository_host.remove_label = MagicMock(
            side_effect=selective_remove
        )

        response = client.post("/api/issues/123/retry")

        # Partial-failure contract: 409 + success=False so the dashboard's
        # success branch (optimistic requeue + "queued for retry" toast)
        # is skipped and the user sees the error path.
        assert response.status_code == 409
        body = response.json()
        assert body["success"] is False
        assert "blocked-failed" in body["failed_labels"]
        assert "blocked" in body["removed_labels"]
        assert "error" in body and body["error"]

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
        # No open PR: Retry clears pr-pending too (#7293 keeps it otherwise).
        mock_orch.repository_host.list_open_prs_complete.return_value = []
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

        # Verify all dismissible labels, including tech_lead provenance, were targeted.
        removed_issue_numbers = [num for num, _ in removed_labels]
        assert all(num == 123 for num in removed_issue_numbers)
        assert {label for _, label in removed_labels} == {
            "blocked",
            "blocked-needs-human",
            "tech-lead-needs-human",
            "blocked-failed",
            "in-progress",
        }

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

    def test_dismiss_reports_an_ordinary_label_removal_failure(
        self, client_with_orchestrator
    ):
        """Dismiss STOPS when an ordinary label removal fails (#6999 F5 r7).

        This asserted ``200 / success: true`` on the theory that an
        already-absent label raises. It does not: ``GitHubAdapter.remove_label``
        treats a 404 as idempotent success and retries transport faults itself,
        so a raise reaching the command means GitHub still carries the label.
        Reporting that as a dismissal - after pruning the board - is exactly the
        GitHub/local disagreement this command was built to prevent.
        """
        from issue_orchestrator.domain.models import SessionHistoryEntry

        client, mock_orch = client_with_orchestrator
        lm = mock_orch.deps.label_manager
        history_entry = SessionHistoryEntry(
            issue_number=123,
            title="Test Issue",
            agent_type="agent:claude",
            status="blocked",
            runtime_minutes=10,
        )
        mock_orch.state.session_history = [history_entry]

        def _remove(number, label):
            if label != lm.needs_human:
                raise Exception("GitHub write failed")

        mock_orch.repository_host.remove_label = MagicMock(side_effect=_remove)

        response = client.post("/api/issues/123/dismiss")

        assert response.status_code == 409
        data = response.json()
        assert data["success"] is False
        assert lm.blocked in data["failed_labels"]
        assert lm.needs_human in data["removed_labels"]
        # ...and the board still shows the issue, because GitHub still does.
        assert mock_orch.state.session_history == [history_entry]


class TestOperatorCommandsRespectTheSharedBlockOwner:
    """Retry and dismiss against a REAL shared-block owner (#6999 F3 round 5).

    The existing endpoint tests use an ungoverned mock, so they cannot see the
    case that matters: a block whose owner refuses to release it. Dismiss used
    to catch that refusal, discard it, prune the issue's queue and history
    state, and report success - leaving ``blocked-needs-human`` on GitHub, the
    issue invisible locally, and an operator told it was done. With tech-lead
    provenance it was worse: the same loop went on to strip the marker, so the
    shared label was left standing with nothing to explain or recover it.

    Both endpoints now consume one owner command, so a refusal means the same
    thing to each of them.
    """

    def _wire_real_block(self, mock_orch, tmp_path, live, *, quarantined=()):
        from issue_orchestrator.control.needs_human_block import NeedsHumanBlock
        from issue_orchestrator.execution.pending_work_claim_store import (
            SqlitePendingWorkClaimStore,
        )

        lm = mock_orch.deps.label_manager

        class _Labels:
            def add_label(self, issue_number: int, label: str) -> None:
                live.setdefault(issue_number, set()).add(label)

            def remove_label(self, issue_number: int, label: str) -> None:
                live.setdefault(issue_number, set()).discard(label)

        mock_orch.deps.needs_human_block = NeedsHumanBlock(
            needs_human_label=lm.needs_human,
            tech_lead_marker=lm.tech_lead_needs_human,
            labels=_Labels(),
            read_labels=lambda number: sorted(live.get(number, set())),
            label_application=standing_while_present(lambda number: sorted(live.get(number, set()))),
            quarantined_issue_numbers=lambda: frozenset(quarantined),
            causes=SqlitePendingWorkClaimStore.for_repo(tmp_path),
        )
        mock_orch.repository_host.remove_label.side_effect = (
            lambda number, label: live.setdefault(number, set()).discard(label)
        )
        mock_orch.repository_host.get_issue_labels.return_value = sorted(
            live.get(123, set())
        )
        return lm

    def test_dismiss_refuses_while_a_quarantine_holds_the_block(
        self, client_with_orchestrator, tmp_path
    ):
        """The exact round-4 failure: refused, yet reported as dismissed."""
        from issue_orchestrator.domain.models import SessionHistoryEntry

        client, mock_orch = client_with_orchestrator
        live = {123: {"blocked", "blocked-needs-human", "in-progress"}}
        lm = self._wire_real_block(mock_orch, tmp_path, live, quarantined=(123,))
        history_entry = SessionHistoryEntry(
            issue_number=123,
            title="Test Issue",
            agent_type="agent:claude",
            status="needs_human",
            runtime_minutes=10,
        )
        mock_orch.state.session_history = [history_entry]

        response = client.post("/api/issues/123/dismiss")

        assert response.status_code == 409
        body = response.json()
        assert body["success"] is False
        assert lm.needs_human in body["failed_labels"]
        assert "claim_quarantine" in body["held_by"]
        # The block is still on GitHub...
        assert lm.needs_human in live[123]
        # ...and the issue is still visible locally, so the operator can see
        # what is holding it rather than losing track of it entirely.
        assert mock_orch.state.session_history == [history_entry]

    def test_dismiss_does_not_strip_the_provenance_of_its_own_refusal(
        self, client_with_orchestrator, tmp_path
    ):
        """Tech-lead provenance survives a refused dismiss.

        The marker is frequently the very reason the shared label could not be
        cleared. Removing it in the same pass left the block standing with
        nothing to explain it and nothing to recover it from.
        """
        client, mock_orch = client_with_orchestrator
        live = {123: {"blocked-needs-human", "tech-lead-needs-human"}}
        lm = self._wire_real_block(mock_orch, tmp_path, live)

        response = client.post("/api/issues/123/dismiss")

        assert response.status_code == 409
        assert "tech_lead_escalation" in response.json()["held_by"]
        assert lm.needs_human in live[123]
        assert lm.tech_lead_needs_human in live[123]

    def test_retry_refuses_while_a_quarantine_holds_the_block(
        self, client_with_orchestrator, tmp_path
    ):
        """Retry reports the same refusal, and keeps its retry gates.

        ``session_history`` is the gate: ``QueueCache.evaluate_issue`` skips any
        issue listed there, so clearing it is what actually requeues the issue.
        Clearing it while GitHub still carries the block is how the planner
        relaunches into a quarantined issue.
        """
        from issue_orchestrator.domain.models import SessionHistoryEntry

        client, mock_orch = client_with_orchestrator
        live = {123: {"blocked", "blocked-needs-human"}}
        lm = self._wire_real_block(mock_orch, tmp_path, live, quarantined=(123,))
        gate = SessionHistoryEntry(
            issue_number=123,
            title="Test Issue",
            agent_type="agent:claude",
            status="needs_human",
            runtime_minutes=10,
        )
        mock_orch.state.session_history = [gate]
        mock_orch.state.failed_this_cycle = {123}

        response = client.post("/api/issues/123/retry")

        assert response.status_code == 409
        body = response.json()
        assert body["success"] is False
        assert lm.needs_human in body["failed_labels"]
        assert "claim_quarantine" in body["held_by"]
        assert lm.needs_human in live[123]
        # The gates are untouched, so the planner still skips the issue.
        assert mock_orch.state.session_history == [gate]
        assert mock_orch.state.failed_this_cycle == {123}

    def test_retry_refuses_and_keeps_the_tech_lead_marker_that_caused_it(
        self, client_with_orchestrator, tmp_path
    ):
        """Retry must not strip the provenance it is being refused over.

        The marker IS the tech-lead lifecycle's record of why the shared block
        exists. Removing it while the block stays leaves a label with nothing
        left to explain it and nothing to recover it from.
        """
        client, mock_orch = client_with_orchestrator
        live = {123: {"blocked", "blocked-needs-human", "tech-lead-needs-human"}}
        lm = self._wire_real_block(mock_orch, tmp_path, live)
        mock_orch.state.session_history = []

        response = client.post("/api/issues/123/retry")

        assert response.status_code == 409
        assert "tech_lead_escalation" in response.json()["held_by"]
        assert lm.needs_human in live[123]
        assert lm.tech_lead_needs_human in live[123]

    def test_retry_succeeds_once_nothing_else_requires_the_block(
        self, client_with_orchestrator, tmp_path
    ):
        """The other side: with no unsettleable cause, retry clears and requeues."""
        from issue_orchestrator.domain.models import SessionHistoryEntry

        client, mock_orch = client_with_orchestrator
        live = {123: {"blocked", "blocked-needs-human"}}
        lm = self._wire_real_block(mock_orch, tmp_path, live)
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Test Issue",
                agent_type="agent:claude",
                status="needs_human",
                runtime_minutes=10,
            )
        ]

        response = client.post("/api/issues/123/retry")

        assert response.status_code == 200
        assert response.json()["success"] is True
        assert lm.needs_human not in live[123]
        assert mock_orch.state.session_history == []

    def test_dismiss_succeeds_once_nothing_else_requires_the_block(
        self, client_with_orchestrator, tmp_path
    ):
        """The refusal is scoped: with no other cause, dismiss clears it."""
        client, mock_orch = client_with_orchestrator
        live = {123: {"blocked", "blocked-needs-human", "in-progress"}}
        lm = self._wire_real_block(mock_orch, tmp_path, live)

        response = client.post("/api/issues/123/dismiss")

        assert response.status_code == 200
        assert response.json()["success"] is True
        assert lm.needs_human not in live[123]


class TestBulkUnblockRunsTheOperatorRetryCommand:
    """The dashboard's bulk "Unblock & Retry" against a REAL shared block (porchpin #400).

    The bulk route used to remove each label with a plain write. The shared
    ``needs-human`` block refused the uncaused write, every other label came off
    (the tech-lead marker included), and the route reported the issue unblocked
    while GitHub still blocked it. It now runs the per-issue retry command, so a
    refusal means the same thing from either button.
    """

    @pytest.fixture
    def bulk_client(self, client_with_orchestrator):
        from issue_orchestrator.entrypoints import web

        _control, mock_orch = client_with_orchestrator
        web.set_orchestrator(mock_orch)
        try:
            yield TestClient(web.app), mock_orch
        finally:
            web.set_orchestrator(None)

    def test_a_refused_block_is_reported_failed_and_keeps_its_marker(
        self, bulk_client, tmp_path
    ):
        client, mock_orch = bulk_client
        live = {123: {"blocked-failed", "blocked-needs-human", "tech-lead-needs-human"}}
        lm = TestOperatorCommandsRespectTheSharedBlockOwner()._wire_real_block(
            mock_orch, tmp_path, live
        )
        mock_orch.request_refresh = MagicMock()

        response = client.post("/api/unblock-retry", json={"issues": [123]})

        assert response.status_code == 200
        body = response.json()
        assert body["unblocked"] == []
        [failure] = body["failed"]
        assert failure["issue"] == 123
        assert "blocked-needs-human is still on the issue" in failure["error"]
        assert "tech_lead_escalation" in failure["error"]
        assert body["refresh_triggered"] is False
        # Nothing after the refused block was touched, so its provenance stays.
        assert live[123] == {"blocked-failed", lm.needs_human, lm.tech_lead_needs_human}
        mock_orch.request_refresh.assert_not_called()

    def test_a_bulk_unblock_of_many_issues_lists_open_prs_once_and_never_searches(
        self, bulk_client
    ):
        """#7293: Retry keeps pr-pending while an open PR belongs to the issue.

        The fact must not cost a GitHub SEARCH per issue: search is rate-limited
        (30/min) far below a bulk unblock of many issues, and a ``#N`` search
        over-matches PRs that merely mention the issue. One complete core-REST
        listing serves the whole request.
        """
        from unittest.mock import PropertyMock

        from issue_orchestrator.control.published_review_custody import (
            NO_PUBLISHED_REVIEW_HOLDS,
        )
        from issue_orchestrator.entrypoints.bootstrap_operator_commands import (
            build_operator_issue_command_factory,
        )
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        client, mock_orch = bulk_client
        issues = list(range(1, 21))
        live = {n: {"blocked-failed", "pr-pending"} for n in issues}
        host = MagicMock()
        host.remove_label.side_effect = lambda n, label: live[n].discard(label)
        host.list_open_prs_complete.return_value = [PRInfo(
            number=900, title="work", url="u", branch="7-work", body="", state="open", labels=[])]
        mock_orch.repository_host = host
        mock_orch.request_refresh = MagicMock()

        class _Fresh:
            def read_issue_labels(self, number: int) -> list[str]:
                return sorted(live[number])

        factory = build_operator_issue_command_factory(
            mock_orch.config, repository_host=host, label_manager=mock_orch.deps.label_manager,
            needs_human_block=mock_orch.deps.needs_human_block, fresh_issue_reader=_Fresh(),
            queue_cache_store=mock_orch.deps.queue_cache_store,
            published_review=NO_PUBLISHED_REVIEW_HOLDS, action_liveness=liveness_owner())
        # As production: the facade composes one command per request.
        type(mock_orch).operator_issue_commands = PropertyMock(
            side_effect=lambda: factory(state=lambda: mock_orch.state, run_locked=lambda fn: fn()))

        response = client.post("/api/unblock-retry", json={"issues": issues})

        assert response.status_code == 200
        assert response.json()["unblocked"] == issues
        host.list_open_prs_complete.assert_called_once_with()
        called = {name for name, _args, _kwargs in host.method_calls}
        assert not {name for name in called if "search" in name or name == "get_prs_for_issue"}
        assert live[7] == {"pr-pending"}, "the issue with an open PR keeps its gate"
        assert all(live[n] == set() for n in issues if n != 7)

    def test_a_releasable_block_is_cleared_and_reported_unblocked(
        self, bulk_client, tmp_path
    ):
        from issue_orchestrator.domain.models import SessionHistoryEntry

        client, mock_orch = bulk_client
        live = {123: {"blocked-failed", "blocked-needs-human"}}
        lm = TestOperatorCommandsRespectTheSharedBlockOwner()._wire_real_block(
            mock_orch, tmp_path, live
        )
        mock_orch.request_refresh = MagicMock()
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Test Issue",
                agent_type="agent:claude",
                status="failed",
                runtime_minutes=10,
            )
        ]

        response = client.post("/api/unblock-retry", json={"issues": [123]})

        assert response.status_code == 200
        body = response.json()
        assert body == {"unblocked": [123], "failed": [], "refresh_triggered": True}
        assert live[123] == set()
        assert lm.needs_human not in live[123]
        assert mock_orch.state.session_history == []
        mock_orch.request_refresh.assert_called_once()


class TestTheRouteMapsTheTypedOutcome:
    """The transport half of the seam (#6999 F6 round 7).

    The command reports FACTS - which labels came off, which would not, what is
    still holding the block - and nothing about HTTP. Turning those into a
    status code and this API's response body is the route's job, so it is
    asserted here against outcomes handed in directly rather than produced by
    driving the command: what is under test is the mapping, not the transition.
    """

    def _answering(self, mock_orch, **fields):
        """Point the route at a command that returns one chosen outcome."""
        from issue_orchestrator.ports.operator_issue_commands import (
            OperatorCommandIntent,
            OperatorCommandOutcome,
            OperatorCommandStatus,
        )

        fields.setdefault("intent", OperatorCommandIntent.RETRY)
        fields.setdefault("status", OperatorCommandStatus.COMMITTED)
        fields.setdefault("issue_number", 123)
        outcome = OperatorCommandOutcome(**fields)

        class _Commands:
            def retry(self, issue_number: int):
                del issue_number
                return outcome

            def dismiss(self, issue_number: int):
                del issue_number
                return outcome

        mock_orch.operator_issue_commands = _Commands()

    def test_a_committed_retry_is_200_and_says_it_was_queued(
        self, client_with_orchestrator
    ):
        client, mock_orch = client_with_orchestrator
        self._answering(mock_orch, removed=("blocked",))

        response = client.post("/api/issues/123/retry")

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["message"] == "Issue #123 queued for retry"
        assert body["removed_labels"] == ["blocked"]

    def test_a_committed_dismiss_is_200_and_says_it_was_dismissed(
        self, client_with_orchestrator
    ):
        """Same status, different words - which is why intent is on the outcome."""
        from issue_orchestrator.ports.operator_issue_commands import (
            OperatorCommandIntent,
        )

        client, mock_orch = client_with_orchestrator
        self._answering(
            mock_orch, intent=OperatorCommandIntent.DISMISS, removed=("blocked",)
        )

        response = client.post("/api/issues/123/dismiss")

        assert response.status_code == 200
        assert response.json()["message"] == "Issue #123 dismissed"

    def test_a_still_blocked_outcome_is_409_and_names_its_holders(
        self, client_with_orchestrator
    ):
        from issue_orchestrator.ports.operator_issue_commands import (
            OperatorCommandStatus,
        )

        client, mock_orch = client_with_orchestrator
        self._answering(
            mock_orch,
            status=OperatorCommandStatus.STILL_BLOCKED,
            blocked="blocked-needs-human",
            held_by=("claim_quarantine",),
        )

        response = client.post("/api/issues/123/retry")

        assert response.status_code == 409
        body = response.json()
        assert body["success"] is False
        assert body["failed_labels"] == ["blocked-needs-human"]
        assert body["held_by"] == ["claim_quarantine"]
        assert "was not retried" in body["error"]
        assert "claim_quarantine still requires it" in body["error"]

    def test_a_still_blocked_outcome_with_no_holder_says_it_would_not_clear(
        self, client_with_orchestrator
    ):
        """Refused by an owner, or simply not written - two different sentences."""
        from issue_orchestrator.ports.operator_issue_commands import (
            OperatorCommandIntent,
            OperatorCommandStatus,
        )

        client, mock_orch = client_with_orchestrator
        self._answering(
            mock_orch,
            intent=OperatorCommandIntent.DISMISS,
            status=OperatorCommandStatus.STILL_BLOCKED,
            blocked="blocked-needs-human",
        )

        response = client.post("/api/issues/123/dismiss")

        assert response.status_code == 409
        body = response.json()
        assert body["held_by"] == []
        assert "could not be cleared" in body["error"]
        assert "was not dismissed" in body["error"]

    def test_an_incomplete_outcome_is_409_and_names_the_failed_labels(
        self, client_with_orchestrator
    ):
        """The status dismiss used to be unable to produce (#6999 F5 round 7)."""
        from issue_orchestrator.ports.operator_issue_commands import (
            OperatorCommandIntent,
            OperatorCommandStatus,
        )

        client, mock_orch = client_with_orchestrator
        self._answering(
            mock_orch,
            intent=OperatorCommandIntent.DISMISS,
            status=OperatorCommandStatus.INCOMPLETE,
            removed=("blocked-needs-human",),
            failed=("blocked",),
        )

        response = client.post("/api/issues/123/dismiss")

        assert response.status_code == 409
        body = response.json()
        assert body["success"] is False
        assert body["failed_labels"] == ["blocked"]
        assert body["removed_labels"] == ["blocked-needs-human"]
        assert "was not dismissed" in body["error"]
        assert "retry the action" in body["error"]
