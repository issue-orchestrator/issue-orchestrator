"""Production-wiring tests for the tech_lead ``reset_retry`` executor (#6777).

These exercise the real composition path — ``build_tech_lead_reset_retry_executor``
→ the shared lifecycle owner’s ``reset_snapshot`` — rather than
a hand-rolled predicate, to prove the reset-freshness check covers EVERY runtime
owner the reset boundary would terminate. A stale proposal must stale-downgrade
with zero cancellation/abandon/reset whenever any hidden runtime is active, even
with no visible ``active_sessions`` entry.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.actions import (
    ActionResultType,
    ResetRetryIssueAction,
)
from issue_orchestrator.control.background_job_supervisor import (
    BackgroundJobSupervisor,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.pr_scanner import PRScanner
from issue_orchestrator.control.tech_lead_reset_retry import STALE_DOWNGRADE_MODE
from issue_orchestrator.domain.models import Issue, OrchestratorState
from issue_orchestrator.entrypoints import web_retry_history_routes as web_mod
from issue_orchestrator.entrypoints.tech_lead_reset_retry_wiring import (
    build_tech_lead_reset_retry_executor,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pending_work_claim_store import UnreadableClaim
from tests.runtime_lifecycle_helpers import runtime_owners

BLOCKED_FAILED = "blocked-failed"
HIDDEN_EXCHANGE_JOB_ID = "review-exchange:17:coding-1:run-1"


def _open_blocking_issue() -> Issue:
    """An open, still-blocking issue so the ONLY stale reason under test is
    active runtime (never a closed/unreadable/unblocked precondition)."""
    return Issue(
        number=17,
        title="Broken issue",
        labels=["agent:test", BLOCKED_FAILED],
        state="open",
        repo="owner/repo",
    )


def _action() -> ResetRetryIssueAction:
    return ResetRetryIssueAction(
        issue_number=17,
        rationale="Worktree unrecoverable; scratch reset is the fix.",
        proposal_id="A2",
        finding_ids=("T1",),
        anchor_issue_number=17,
    )


class _StubRunner:
    """Background runner whose submitted jobs stay 'running' until drained."""

    def __init__(self) -> None:
        self.running: set[str] = set()

    def submit(self, job_id: str, fn) -> bool:
        del fn
        self.running.add(job_id)
        return True

    def is_running(self, job_id: str) -> bool:
        return job_id in self.running

    def drain_completed(self) -> list:
        return []


class _FakePairRegistry:
    """Persistent-pair registry that reports one issue active and records
    every ``release`` so a test can prove the reset did not tear the pair down."""

    def __init__(self, active_issue: int | None = None) -> None:
        self._active_issue = active_issue
        self.released: list[tuple[int, str]] = []

    def has_active_pair(self, issue_key) -> bool:
        return issue_key == self._active_issue

    def release(self, issue_key, *, reason: str) -> None:
        self.released.append((issue_key, reason))

    def shutdown_all(self, *, reason: str) -> None:  # pragma: no cover - unused
        del reason


class _FakePublishRecovery:
    """Publish-retry owner reporting one issue active and recording abandons."""

    def __init__(self, active_issue: int | None = None, raises: bool = False) -> None:
        self._active_issue = active_issue
        self._raises = raises
        self.abandoned: list[int] = []

    def has_active_retry(self, issue_number: int) -> bool:
        if self._raises:
            raise RuntimeError("publish-retry owner cannot be queried")
        return issue_number == self._active_issue

    def abandon_issue(self, issue_number: int) -> None:
        self.abandoned.append(issue_number)


def _build(
    monkeypatch,
    *,
    pair_registry=None,
    job_supervisor=None,
    publish_recovery=None,
    session_manager=None,
    active_sessions=(),
):
    """Build the executor through the production wiring over a lightweight
    orchestrator carrying real runtime owners. Returns (executor, reset_spy)."""
    state = OrchestratorState()
    state.active_sessions = list(active_sessions)
    repository_host = MagicMock()
    repository_host.get_issue.return_value = _open_blocking_issue()
    services = SimpleNamespace(
        pair_registry=pair_registry,
        background_job_supervisor=job_supervisor,
    )
    deps = SimpleNamespace(
        label_manager=LabelManager(Config()),
        events=MagicMock(),
        repository_host=repository_host,
        queue_cache_store=MagicMock(),
        session_manager=session_manager,
        publish_recovery=publish_recovery,
        services=services,
        runtime_lifecycle=runtime_owners(
            session_manager=session_manager,
            active_sessions=state.active_sessions,
            pair_registry=pair_registry,
            job_supervisor=job_supervisor,
            publish_recovery=publish_recovery,
        ),
    )
    orchestrator = SimpleNamespace(
        deps=deps,
        config=Config(),
        state=state,
        repository_host=repository_host,
    )
    # Spy the reused reset pipeline: if it is ever called, the reset boundary
    # (and thus every termination/abandon) ran. A stale downgrade must not call
    # it at all, so ``assert_not_called`` is a zero-mutation proof.
    reset_spy = MagicMock(return_value=({"queued_now": True}, None))
    monkeypatch.setattr(web_mod, "reset_and_retry_issue", reset_spy)
    executor = build_tech_lead_reset_retry_executor(orchestrator)
    return executor, reset_spy


def test_hidden_supervised_exchange_job_downgrades_with_zero_mutation(monkeypatch):
    """A hidden supervised review-exchange job (no visible active_sessions entry)
    stales the proposal: it downgrades and the job is NOT cancelled."""
    supervisor = BackgroundJobSupervisor(_StubRunner())
    assert supervisor.submit(HIDDEN_EXCHANGE_JOB_ID, lambda: None, timeout_seconds=600)
    executor, reset_spy = _build(monkeypatch, job_supervisor=supervisor)

    result = executor.apply(_action())

    assert result.result_type is ActionResultType.SKIPPED
    assert result.details["mode"] == STALE_DOWNGRADE_MODE
    # Zero mutation: no reset ran (so no terminate_issue_runtime), and the
    # hidden exchange job is still running and un-cancelled.
    reset_spy.assert_not_called()
    assert supervisor.is_running(HIDDEN_EXCHANGE_JOB_ID)
    assert not supervisor.status(HIDDEN_EXCHANGE_JOB_ID).cancelled


def test_hidden_persistent_pair_downgrades_without_release(monkeypatch):
    """A hidden persistent coder/reviewer pair stales the proposal: it
    downgrades and ``release`` is never called."""
    pair_registry = _FakePairRegistry(active_issue=17)
    executor, reset_spy = _build(monkeypatch, pair_registry=pair_registry)

    result = executor.apply(_action())

    assert result.result_type is ActionResultType.SKIPPED
    assert result.details["mode"] == STALE_DOWNGRADE_MODE
    reset_spy.assert_not_called()
    assert pair_registry.released == []


def test_inflight_publish_retry_downgrades_without_abandon(monkeypatch):
    """An in-flight publish retry stales the proposal: it downgrades and
    ``publish_recovery.abandon_issue`` is never called."""
    publish_recovery = _FakePublishRecovery(active_issue=17)
    executor, reset_spy = _build(monkeypatch, publish_recovery=publish_recovery)

    result = executor.apply(_action())

    assert result.result_type is ActionResultType.SKIPPED
    assert result.details["mode"] == STALE_DOWNGRADE_MODE
    reset_spy.assert_not_called()
    assert publish_recovery.abandoned == []


def test_unqueryable_owner_downgrades_fail_safe(monkeypatch):
    """Fail-safe: an owner that raises when queried is treated as possibly
    active, so the proposal downgrades rather than resetting on unverifiable
    state (never silent-wrong)."""
    publish_recovery = _FakePublishRecovery(raises=True)
    executor, reset_spy = _build(monkeypatch, publish_recovery=publish_recovery)

    result = executor.apply(_action())

    assert result.result_type is ActionResultType.SKIPPED
    assert result.details["mode"] == STALE_DOWNGRADE_MODE
    reset_spy.assert_not_called()
    assert publish_recovery.abandoned == []


def test_no_active_runtime_executes_reset(monkeypatch):
    """No-regression: with genuinely no active runtime owner, the freshness
    check passes and the production reset pipeline is invoked from scratch."""
    supervisor = BackgroundJobSupervisor(_StubRunner())  # no jobs submitted
    executor, reset_spy = _build(monkeypatch, job_supervisor=supervisor)

    result = executor.apply(_action())

    assert result.result_type is ActionResultType.SUCCESS
    reset_spy.assert_called_once()
    assert reset_spy.call_args.kwargs["issue_number"] == 17
    assert reset_spy.call_args.kwargs["from_scratch"] is True


# -- release_withheld_review through its production wiring (#7399) ------------


def _build_release(*, job_supervisor=None, history=(), unreadable=()):
    from datetime import datetime, timezone

    from issue_orchestrator.control.action_results import ActionResult
    from issue_orchestrator.domain.models import SessionHistoryEntry
    from issue_orchestrator.entrypoints.tech_lead_reset_retry_wiring import (
        build_tech_lead_review_release_executor,
    )
    from issue_orchestrator.ports.pull_request_tracker import PRInfo, StatusCheckRollupRead

    config = Config()
    config.repo = "owner/repo"
    config.code_review_label = "needs-code-review"
    config.code_review_agent = "agent:reviewer"
    state = OrchestratorState()
    state.session_history = [
        SessionHistoryEntry(issue_number=17, title="t", agent_type="agent:test", status=status,
                            runtime_minutes=1, completed_at=datetime(2026, 9, 27, 15, tzinfo=timezone.utc))
        for status in history
    ]
    host = MagicMock()
    host.get_issue.return_value = Issue(
        number=17, title="t", labels=[BLOCKED_FAILED, "pr-pending"], state="open", repo="owner/repo")
    live = PRInfo(number=18, title="#17", url="u", branch="17-work", body="Closes #17", state="open",
                  labels=["needs-code-review"], head_sha="a" * 40)
    # The real complete listing carries no labels; the fresh read does.
    host.list_open_prs_complete.return_value = [replace(live, labels=[])]
    host.get_pr.side_effect = lambda number: live if number == 18 else None
    host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead("SUCCESS")
    applier = MagicMock()
    applier.apply.side_effect = ActionResult.ok
    deps = SimpleNamespace(
        label_manager=LabelManager(config), events=MagicMock(), action_applier=applier,
        # The real scanner: its discovery gate and branch map are what the
        # release asks.
        pr_scanner=PRScanner(config=config, repository=host, events=MagicMock()),
        pending_work_claims=SimpleNamespace(
            list_unresolved_claims=lambda: (), list_unreadable_claims=lambda: unreadable),
        runtime_lifecycle=runtime_owners(active_sessions=state.active_sessions, job_supervisor=job_supervisor),
    )
    orchestrator = SimpleNamespace(deps=deps, config=config, state=state)
    return build_tech_lead_review_release_executor(orchestrator, host), applier


def _release_action():
    from issue_orchestrator.control.actions import ReleaseWithheldReviewAction

    return ReleaseWithheldReviewAction(issue_number=17, proposal_id="A2", anchor_issue_number=17,
                                       observed_at="2026-09-27T14:00:00+00:00",
                                       source_session_name="tech-lead-17")


def test_release_wiring_releases_through_the_guarded_applier():
    executor, applier = _build_release()

    result = executor.apply(_release_action())

    assert result.success, result.error
    assert [type(call.args[0]).__name__ for call in applier.apply.call_args_list] == [
        "AddLabelAction", "AddLabelAction", "RemoveLabelAction"]


def test_release_wiring_sees_hidden_runtime_and_newer_history():
    supervisor = BackgroundJobSupervisor(_StubRunner())
    assert supervisor.submit(HIDDEN_EXCHANGE_JOB_ID, lambda: None, timeout_seconds=600)
    busy, busy_applier = _build_release(job_supervisor=supervisor)
    failed, failed_applier = _build_release(history=("failed",))
    claimed, claimed_applier = _build_release(unreadable=(UnreadableClaim(
        "run", "rework-17", 17, "payload corrupt", "2026-09-27T13:00:00+00:00"),))

    assert busy.apply(_release_action()).details["refusal"] == "live_session"
    assert failed.apply(_release_action()).details["refusal"] == "newer_failure"
    assert claimed.apply(_release_action()).details["refusal"] == "work_claimed"
    busy_applier.apply.assert_not_called()
    failed_applier.apply.assert_not_called()
    claimed_applier.apply.assert_not_called()
