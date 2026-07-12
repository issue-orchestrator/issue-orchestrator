"""Tests for the triage reset_retry execution owner (#6764, ADR-0031 §2)."""

from unittest.mock import ANY, MagicMock

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

    def _arrange(
        self,
        tmp_path,
        executor,
        *,
        seed_failed: bool = True,
        config=None,
        observer=None,
        repository_host=None,
        labels=None,
    ):
        """Arrange a COMPLETED triage completion whose one action is a mandated
        reset, returning ``(state, run)`` so callers can assert on pre-run state
        before invoking ``run()``. The agent always reports ``completed``; the
        injected ``executor`` decides whether the mandated reset commits."""
        session = self._session(tmp_path)
        state = OrchestratorState()
        state.active_sessions = [session]
        if seed_failed:
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
            labels=labels if labels is not None else MagicMock(),
            sessions=MagicMock(),
            events=MagicMock(),
            repository_host=repository_host,
        )
        applier.triage_reset_retry = executor

        if config is None:
            config = MagicMock()
            config.code_review_agent = None

        session_output = MagicMock(spec=SessionOutput)
        session_output.attach_claude_log.return_value = None

        def run() -> None:
            handle_session_completion(
                session=session,
                status=SessionStatus.COMPLETED,
                state=state,
                completion_handler=completion_handler,
                action_applier=applier,
                observer=observer if observer is not None else MagicMock(),
                worktree_manager=None,
                kill_session_fn=lambda _x: None,
                config=config,
                session_output=session_output,
            )

        return state, run

    def _run_completion(self, tmp_path, executor) -> OrchestratorState:
        state, run = self._arrange(tmp_path, executor)
        run()
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

    def test_failed_mandated_reset_routes_effective_failure_end_to_end(self, tmp_path):
        """A COMPLETED agent whose mandated reset FAILED at apply time is
        terminalized as FAILED and routed through EVERY post-apply consumer,
        with no success effect surviving (#6764 re-review F2).

        This begins with an EMPTY failure gate and asserts the completion path
        itself adds the issue — unlike the prior version, which pre-seeded
        ``failed_this_cycle`` and so passed even though the failed-reset path
        never routed anything."""
        executor, _events, run_reset = make_executor(
            outcome=ResetRetryRunOutcome(
                success=False, error="branch delete exploded"
            )
        )
        observer = MagicMock()
        repository_host = MagicMock()
        labels = MagicMock()
        labels.has_label.return_value = False  # let the needs-human add proceed

        state, run = self._arrange(
            tmp_path,
            executor,
            seed_failed=False,
            config=Config(),
            observer=observer,
            repository_host=repository_host,
            labels=labels,
        )
        # Begins WITHOUT the failure gate: the completion path must add it.
        assert 17 not in state.failed_this_cycle

        run()

        run_reset.assert_called_once()

        # Effective terminal status is FAILED across the whole post-apply phase.
        # Observer observed the failure, not the agent-reported success:
        observer.handle_completion.assert_called_once_with(ANY, SessionStatus.FAILED)

        # Failure discovery recorded the failure as a fact:
        [failure] = state.discovered_failures
        assert failure.issue_number == 17
        assert failure.failure_reason == "failed"

        # Retry gate was set BY the completion path (started empty above):
        assert 17 in state.failed_this_cycle

        # Immediate cleanup reason reflects the effective failure, not "completed":
        [cleanup] = state.immediate_cleanups
        assert cleanup.reason == "failed"

        # History terminalized as FAILED with the reset-owner error, never the
        # agent's "completed" intent:
        [entry] = state.session_history
        assert entry.issue_number == 17
        assert entry.status == "failed"
        assert "branch delete exploded" in (entry.status_reason or "")

        # Durable, crash-safe operator surface via the existing label/comment
        # action owners (needs-human label + explanatory comment on the issue):
        labels.add_label.assert_any_call(17, "needs-human")
        assert repository_host.add_comment.call_count == 1
        comment_number, comment_body = repository_host.add_comment.call_args.args
        assert comment_number == 17
        assert "Reset & Retry Did Not Complete" in comment_body
        assert "branch delete exploded" in comment_body

        # No success effect survived: the reset-success side effect (making the
        # issue retryable, which prunes its own history entry and clears the
        # failure gate) did NOT run, so the FAILED record and gate both stand.
        assert any(e.issue_number == 17 for e in state.session_history)
