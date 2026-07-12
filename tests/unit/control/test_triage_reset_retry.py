"""Tests for the triage reset_retry execution owner (#6764, ADR-0031 §2)."""

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import (
    ActionResultType,
    ResetRetryIssueAction,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.session_completion import handle_session_completion
from issue_orchestrator.control.triage_reset_retry import (
    STALE_DOWNGRADE_MODE,
    ResetRetryRunOutcome,
    TriageResetRetryExecutor,
    preserve_reset_retry_eligibility,
    reset_retry_stale_reason,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    OrchestratorState,
    Session,
    SessionHistoryEntry,
    SessionStatus,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.session_output import SessionOutput
from tests.unit.session_run_helpers import make_session_run_assets

BLOCKED_FAILED = "blocked-failed"


def make_action(**overrides) -> ResetRetryIssueAction:
    defaults = dict(
        issue_number=17,
        rationale="Worktree unrecoverable; scratch reset is the fix.",
        proposal_id="A2",
        finding_ids=("T1",),
        anchor_issue_number=17,
        reason="triage decision action A2: reset and retry from scratch",
    )
    defaults.update(overrides)
    return ResetRetryIssueAction(**defaults)


def make_issue(**overrides) -> Issue:
    defaults = dict(
        number=17,
        title="Broken issue",
        labels=["agent:test", BLOCKED_FAILED],
        state="open",
        repo="owner/repo",
    )
    defaults.update(overrides)
    return Issue(**defaults)


def make_executor(
    *,
    issue: Issue | None = ...,
    active_session: bool = False,
    outcome: ResetRetryRunOutcome | None = None,
) -> tuple[TriageResetRetryExecutor, MagicMock, MagicMock]:
    """Executor with recording events + run_reset fakes."""
    events = MagicMock()
    run_reset = MagicMock(
        return_value=outcome
        if outcome is not None
        else ResetRetryRunOutcome(success=True, details={"queued_now": True})
    )
    resolved_issue = make_issue() if issue is ... else issue
    executor = TriageResetRetryExecutor(
        events=events,
        label_manager=LabelManager(Config()),
        read_issue=lambda _n: resolved_issue,
        has_active_session=lambda _n: active_session,
        run_reset=run_reset,
    )
    return executor, events, run_reset


def published(events: MagicMock, name: EventName) -> list:
    return [
        call.args[0]
        for call in events.publish.call_args_list
        if call.args[0].name == name.value
    ]


class TestStaleReason:
    """Pure precondition policy."""

    def test_valid_when_open_blocked_and_idle(self):
        reason = reset_retry_stale_reason(
            issue=make_issue(),
            active_session=False,
            label_manager=LabelManager(Config()),
        )
        assert reason is None

    def test_unreadable_issue_is_stale(self):
        reason = reset_retry_stale_reason(
            issue=None,
            active_session=False,
            label_manager=LabelManager(Config()),
        )
        assert reason is not None and "could not be read" in reason

    def test_closed_issue_is_stale(self):
        reason = reset_retry_stale_reason(
            issue=make_issue(state="closed"),
            active_session=False,
            label_manager=LabelManager(Config()),
        )
        assert reason is not None and "closed" in reason

    def test_active_session_is_stale(self):
        reason = reset_retry_stale_reason(
            issue=make_issue(),
            active_session=True,
            label_manager=LabelManager(Config()),
        )
        assert reason is not None and "active session" in reason

    def test_no_blocking_label_is_stale(self):
        reason = reset_retry_stale_reason(
            issue=make_issue(labels=["agent:test"]),
            active_session=False,
            label_manager=LabelManager(Config()),
        )
        assert reason is not None and "blocking-class" in reason


class TestExecutorApply:
    def test_valid_preconditions_invoke_reset_owner(self):
        executor, events, run_reset = make_executor()

        result = executor.apply(make_action())

        assert result.result_type == ActionResultType.SUCCESS
        run_reset.assert_called_once_with(17, ["agent:test", BLOCKED_FAILED])
        [executed] = published(events, EventName.TRIAGE_ACTION_EXECUTED)
        assert executed.data["issue_number"] == 17  # anchor
        assert executed.data["target_number"] == 17
        assert executed.data["action_id"] == "A2"
        assert executed.data["proposal_type"] == "reset_retry"
        assert executed.data["boundary"] == {"queued_now": True}
        assert not published(events, EventName.TRIAGE_ACTION_PROPOSED)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"active_session": True},
            {"issue": None},
            {"issue": Issue(number=17, title="t", labels=["agent:test"], state="closed", repo="o/r")},
            {"issue": Issue(number=17, title="t", labels=["agent:test"], repo="o/r")},
        ],
        ids=["active-session", "unreadable", "closed", "no-blocking-label"],
    )
    def test_stale_precondition_downgrades_without_mutation(self, kwargs):
        executor, events, run_reset = make_executor(**kwargs)

        result = executor.apply(make_action())

        assert result.result_type == ActionResultType.SKIPPED
        assert result.details["mode"] == STALE_DOWNGRADE_MODE
        run_reset.assert_not_called()
        [surfaced] = published(events, EventName.TRIAGE_ACTION_PROPOSED)
        assert surfaced.data["mode"] == STALE_DOWNGRADE_MODE
        assert surfaced.data["stale_reason"]
        assert surfaced.data["action_id"] == "A2"
        assert surfaced.data["target_number"] == 17
        assert surfaced.data["body_preview"].startswith("Worktree unrecoverable")
        assert not published(events, EventName.TRIAGE_ACTION_EXECUTED)

    def test_downgrade_event_reports_on_anchor_issue(self):
        executor, events, _ = make_executor(active_session=True)

        executor.apply(make_action(issue_number=17, anchor_issue_number=99))

        [surfaced] = published(events, EventName.TRIAGE_ACTION_PROPOSED)
        assert surfaced.data["issue_number"] == 99
        assert surfaced.data["target_number"] == 17

    def test_owner_failure_fails_loudly(self):
        executor, events, run_reset = make_executor(
            outcome=ResetRetryRunOutcome(success=False, error="branch delete exploded")
        )

        result = executor.apply(make_action())

        assert result.result_type == ActionResultType.FAILURE
        assert "branch delete exploded" in (result.error or "")
        assert "A2" in (result.error or "")
        run_reset.assert_called_once()
        assert not published(events, EventName.TRIAGE_ACTION_EXECUTED)
        assert not published(events, EventName.TRIAGE_ACTION_PROPOSED)


class TestApplierDispatch:
    def _applier(self, executor=None) -> tuple[ActionApplier, MagicMock]:
        events = MagicMock()
        applier = ActionApplier(
            labels=MagicMock(),
            sessions=MagicMock(),
            events=events,
        )
        applier.triage_reset_retry = executor
        return applier, events

    def test_dispatch_routes_to_executor(self):
        executor, _events, run_reset = make_executor()
        applier, _ = self._applier(executor)

        result = applier.apply(make_action())

        assert result.result_type == ActionResultType.SUCCESS
        run_reset.assert_called_once()

    def test_unwired_executor_fails_loudly(self):
        applier, _ = self._applier(executor=None)

        result = applier.apply(make_action())

        assert result.result_type == ActionResultType.FAILURE
        assert "no" in (result.error or "") and "wired" in (result.error or "")


class TestPreserveEligibility:
    def test_successful_reset_actions_are_recleared(self):
        executor, _events, _run_reset = make_executor()
        ok = executor.apply(make_action(issue_number=17))
        make_retryable = MagicMock()

        cleared = preserve_reset_retry_eligibility(
            [ok], make_retryable=make_retryable
        )

        assert cleared == [17]
        make_retryable.assert_called_once_with(17)

    def test_downgraded_and_foreign_results_are_ignored(self):
        stale_executor, _e, _r = make_executor(active_session=True)
        downgraded = stale_executor.apply(make_action())
        failed_executor, _e2, _r2 = make_executor(
            outcome=ResetRetryRunOutcome(success=False, error="boom")
        )
        failed = failed_executor.apply(make_action())
        make_retryable = MagicMock()

        cleared = preserve_reset_retry_eligibility(
            [downgraded, failed], make_retryable=make_retryable
        )

        assert cleared == []
        make_retryable.assert_not_called()


class TestCompletionPipelineEligibility:
    """The completion pipeline must not re-block an issue its own actions
    just reset: the history entry appended after apply_all would otherwise
    silently gate the relaunch (#6764)."""

    def _session(self, tmp_path) -> Session:
        issue = Issue(
            number=17,
            title="Broken issue",
            labels=["agent:triage"],
            repo="owner/repo",
        )
        return Session(
            key=SessionKey(issue=FakeIssueKey("17"), task=TaskKind.CODE),
            issue=issue,
            agent_config=AgentConfig(
                prompt_path=tmp_path / "prompt.md", timeout_minutes=45
            ),
            terminal_id="issue-17",
            worktree_path=tmp_path / "worktree",
            branch_name="17-fix",
            run_assets=make_session_run_assets(
                tmp_path / "worktree", session_name="issue-17"
            ),
        )

    def _run_completion(self, tmp_path, executor) -> OrchestratorState:
        session = self._session(tmp_path)
        state = OrchestratorState()
        state.active_sessions = [session]
        state.failed_this_cycle.add(17)

        completion_handler = MagicMock()
        completion_handler.process_completion.return_value = MagicMock(
            actions=[make_action(issue_number=17, anchor_issue_number=17)],
            history_entry=SessionHistoryEntry(
                issue_number=17,
                title="Broken issue",
                agent_type="agent:triage",
                status="completed",
                runtime_minutes=3,
                pr_url=None,
            ),
            should_defer_cleanup=False,
            pending_cleanup=None,
            should_queue_review=False,
            pr_url=None,
            pr_number=None,
        )
        applier = ActionApplier(
            labels=MagicMock(), sessions=MagicMock(), events=MagicMock()
        )
        applier.triage_reset_retry = executor

        config = MagicMock()
        config.code_review_agent = None
        handle_session_completion(
            session=session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=completion_handler,
            action_applier=applier,
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda _x: None,
            config=config,
            session_output=MagicMock(spec=SessionOutput),
        )
        return state

    def test_successful_reset_survives_the_history_append(self, tmp_path):
        executor, _events, run_reset = make_executor()

        state = self._run_completion(tmp_path, executor)

        run_reset.assert_called_once()
        assert all(e.issue_number != 17 for e in state.session_history), (
            "the completion's own history entry must not re-block the reset issue"
        )
        assert 17 not in state.failed_this_cycle

    def test_downgraded_reset_keeps_the_history_entry(self, tmp_path):
        executor, _events, run_reset = make_executor(active_session=True)

        state = self._run_completion(tmp_path, executor)

        run_reset.assert_not_called()
        assert any(e.issue_number == 17 for e in state.session_history), (
            "a downgraded proposal posts no mutations - history must be intact"
        )

    def test_reset_failure_suppresses_success_and_routes_failure(self, tmp_path):
        """A reset-owner failure must not record a clean completion.

        The mandated reset is the whole point of the investigation; when it
        fails at apply time the completion routes to a FAILED terminal record
        instead of the agent's 'completed' intent — the single authoritative
        outcome boundary, never a partial reset masked as success
        (#6764 re-review F2)."""
        executor, _events, run_reset = make_executor(
            outcome=ResetRetryRunOutcome(
                success=False, error="branch delete exploded"
            )
        )

        state = self._run_completion(tmp_path, executor)

        run_reset.assert_called_once()
        [entry] = state.session_history
        assert entry.issue_number == 17
        assert entry.status == "failed", (
            "a failed mandated reset must suppress the completed success record"
        )
        assert "branch delete exploded" in (entry.status_reason or "")
        # The reset never ran, so its issue is not re-cleared for relaunch.
        assert 17 in state.failed_this_cycle
