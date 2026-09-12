"""Tests for issue-scoped runtime lifecycle boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from issue_orchestrator.events import EventName
from issue_orchestrator.control.review_exchange_lifecycle import (
    ValidatedWorkCustodyUnproven,
)






@pytest.fixture
def completion_intake(completion_intake_fixture):
    accepted = completion_intake_fixture.accept(230)
    yield completion_intake_fixture.runtime
    completion_intake_fixture.assert_closed_and_drained(accepted)


class _FakeSessionManager:
    def __init__(self, running: set[str]) -> None:
        self.running = set(running)
        self.stopped: list[str] = []

    def exists(self, ref) -> bool:  # noqa: ANN001 - protocol-shaped fake
        return ref.name in self.running

    def stop(self, ref) -> None:  # noqa: ANN001 - protocol-shaped fake
        self.stopped.append(ref.name)
        self.running.discard(ref.name)


def _active_session(terminal_id: str):
    return SimpleNamespace(terminal_id=terminal_id)


class _FakePublishRetryAbandoner:
    def __init__(self) -> None:
        self.abandoned: list[int] = []

    def abandon_issue(self, issue_number: int) -> None:
        self.abandoned.append(issue_number)


def test_terminate_issue_runtime_abandons_publish_retry(completion_intake) -> None:
    """The shared boundary must also abandon in-flight publish retries."""
    publish_recovery = _FakePublishRetryAbandoner()

    runtime_owners(completion_intake=completion_intake, pair_registry=None, job_supervisor=None, publish_recovery=publish_recovery).terminate(230, "issue-completed")

    assert publish_recovery.abandoned == [230]


def test_terminate_issue_runtime_without_publish_recovery_is_noop(
    completion_intake,
) -> None:
    """Omitting the abandoner keeps the boundary working (backward compatible)."""
    result = runtime_owners(completion_intake=completion_intake, pair_registry=None, job_supervisor=None).terminate(230, "issue-completed")

    assert result.issue_number == 230


def test_terminate_issue_runtime_stops_issue_rework_and_hidden_exchange(
    completion_intake,
) -> None:
    pair_registry = Mock()
    job_supervisor = Mock()
    job_supervisor.cancel_matching.return_value = ["review-exchange:230:coding-1"]
    session_manager = _FakeSessionManager({"issue-230", "rework-230", "issue-999"})
    active_sessions = [
        _active_session("issue-230"),
        _active_session("rework-230"),
        _active_session("review-77"),
        _active_session("issue-999"),
    ]

    result = runtime_owners(completion_intake=completion_intake, pair_registry=pair_registry, job_supervisor=job_supervisor, session_manager=session_manager, active_sessions=active_sessions).terminate(230, "reset-retry")

    pair_registry.release.assert_called_once_with(230, reason="reset-retry")
    job_supervisor.cancel_matching.assert_called_once()
    predicate = job_supervisor.cancel_matching.call_args.args[0]
    assert predicate("review-exchange:230:coding-1")
    assert not predicate("review-exchange:231:coding-1")
    assert session_manager.stopped == ["issue-230", "rework-230"]
    assert result.stopped_session_ids == ("issue-230", "rework-230")
    assert result.cleared_active_session_ids == ("issue-230", "rework-230")
    assert result.cancelled_job_ids == ("review-exchange:230:coding-1",)
    assert [session.terminal_id for session in active_sessions] == [
        "review-77",
        "issue-999",
    ]


def test_terminate_issue_runtime_clears_stale_active_session_records(
    completion_intake,
) -> None:
    session_manager = _FakeSessionManager(set())
    active_sessions = [_active_session("issue-230"), _active_session("issue-231")]

    result = runtime_owners(completion_intake=completion_intake, pair_registry=None, job_supervisor=None, session_manager=session_manager, active_sessions=active_sessions).terminate(230, "issue-completed")

    assert session_manager.stopped == []
    assert result.stopped_session_ids == ()
    assert result.cleared_active_session_ids == ("issue-230",)
    assert [session.terminal_id for session in active_sessions] == ["issue-231"]


def test_terminate_issue_runtime_requires_session_manager_for_active_records(
    completion_intake,
) -> None:
    pair_registry = Mock()

    with pytest.raises(RuntimeError, match="without a SessionManager"):
        runtime_owners(completion_intake=completion_intake, pair_registry=pair_registry, job_supervisor=None, session_manager=None, active_sessions=[_active_session("issue-230")]).terminate(230, "reset-retry")

    pair_registry.release.assert_not_called()

from tests.runtime_lifecycle_helpers import runtime_owners


# --------------------------------------------------------------------------
# Teardown captures must not abort the transition that calls them (#7255).
# --------------------------------------------------------------------------


def _failing_owners(**kwargs):
    """Runtime owners whose validated-work capture raises the way #7255 did.

    `ValidatedWorkKey.__post_init__` rejected an empty `repo_slug`, deep inside
    `dispose_at_termination`, and the ValueError escaped to whoever was tearing
    the session down.
    """
    owners = runtime_owners(completion_intake=None, **kwargs)
    owners.validated_work.dispose_at_termination.side_effect = ValueError(
        "repo_slug must be non-empty text"
    )
    return owners


def test_terminate_refuses_on_a_capture_fault() -> None:
    """STRICT by design, and this pins it.

    `terminate` stops live terminals and releases the exchange pair. A capture
    fault means custody is unproven, so tearing down anyway is how validated work
    gets destroyed. It must refuse with nothing stopped.
    """
    session_manager = _FakeSessionManager({"issue-230"})
    active_sessions = [_active_session("issue-230")]
    owners = _failing_owners(
        pair_registry=None, job_supervisor=None,
        session_manager=session_manager, active_sessions=active_sessions,
    )

    with pytest.raises(ValueError, match="repo_slug"):
        owners.terminate(230, "operator-terminated")

    assert session_manager.stopped == []
    assert [s.terminal_id for s in active_sessions] == ["issue-230"]


def test_cancel_exchange_refuses_on_a_capture_fault() -> None:
    """Releasing the pair registry kills live coder/reviewer processes."""
    pair_registry = Mock()
    owners = _failing_owners(pair_registry=pair_registry, job_supervisor=None)

    with pytest.raises(ValueError, match="repo_slug"):
        owners.cancel_exchange(230, "operator-terminated")

    pair_registry.release.assert_not_called()


def test_preserve_completed_terminal_survives_a_capture_fault() -> None:
    """Session completion continues instead of stranding.

    The fault yields None, NOT `ValidatedWorkDispositionBatch.no_work(...)`. A
    fault is not "nothing to preserve": `no_work` reports found_work=False and
    unresolved=False, and that batch is this repo's custody oracle
    (`require_reset` raises on `batch.unresolved`). Handing one back would let a
    later caller read "all clear" from a question that was never answered.
    """
    batch = _failing_owners(
        pair_registry=None, job_supervisor=None,
    ).preserve_completed_terminal(230, "issue-230", "session-completion")

    assert batch is None


def test_capture_fault_is_reported_as_an_event() -> None:
    """Silent degradation would hide the next one; the UI hears about it."""
    owners = _failing_owners(pair_registry=None, job_supervisor=None)

    owners.preserve_completed_terminal(230, "issue-230", "session-completion")

    published = [call.args[0] for call in owners.events.publish.call_args_list]
    assert any(
        event.name == EventName.VALIDATED_WORK_CAPTURE_FAILED.value
        for event in published
    )


def test_preserve_still_raises_so_cleanup_cannot_delete_unpreserved_work() -> None:
    """The strict contract stays the default.

    `preserve` runs before a worktree is deleted and feeds `require_reset`.
    Swallowing a fault here would let a reset or a delete proceed over validated
    work that was never captured -- the loss this subsystem exists to prevent.
    """
    owners = _failing_owners(pair_registry=None, job_supervisor=None)

    with pytest.raises(ValueError, match="repo_slug"):
        owners.preserve(230, "worktree-cleanup")


def test_require_reset_still_raises_on_a_capture_fault() -> None:
    """A reset gate must never read a capture fault as "no work to lose"."""
    owners = _failing_owners(pair_registry=None, job_supervisor=None)

    with pytest.raises(ValueError, match="repo_slug"):
        owners.require_reset(230, "maintenance-reset")


# --------------------------------------------------------------------------
# The whole-issue terminal boundary (#7255).
# --------------------------------------------------------------------------


def _session(terminal_id: str, issue_number: int):
    # `run_assets` matters: the exact-run preserve scopes the evidence read to
    # this one issue instead of sweeping the whole ledger.
    return SimpleNamespace(
        terminal_id=terminal_id,
        issue=SimpleNamespace(number=issue_number),
        run_assets=None,
    )


def test_terminate_every_session_stops_a_review_terminal(completion_intake) -> None:
    """`terminate` alone cannot: it is ISSUE/REWORK-scoped and builds refs from
    the ISSUE number, while a review terminal is `review-<PR number>`.

    Relying on it orphans a live reviewer agent while the caller goes on to
    label the issue blocked-failed.
    """
    session_manager = _FakeSessionManager({"review-4124"})
    active = [_session("review-4124", 230)]

    outcome = runtime_owners(
        completion_intake=completion_intake, pair_registry=None, job_supervisor=None,
        session_manager=session_manager, active_sessions=active,
    ).terminate_every_session(230, "operator-terminated")

    assert session_manager.stopped == ["review-4124"]
    assert outcome.stopped_session_ids == ("review-4124",)
    assert outcome.complete


def test_terminate_every_session_reports_a_terminal_it_could_not_stop(
    completion_intake,
) -> None:
    """A terminal that would not stop is still alive; the result must say so."""
    session_manager = _FakeSessionManager({"review-4124"})
    session_manager.stop = Mock(side_effect=RuntimeError("tmux is gone"))
    active = [_session("review-4124", 230)]

    outcome = runtime_owners(
        completion_intake=completion_intake, pair_registry=None, job_supervisor=None,
        session_manager=session_manager, active_sessions=active,
    ).terminate_every_session(230, "operator-terminated")

    assert not outcome.complete
    assert outcome.failures == (("review-4124", "tmux is gone"),)
    assert "review-4124: tmux is gone" in outcome.failure_details


def test_terminate_every_session_refuses_before_destroying_anything() -> None:
    """Custody first. A capture fault must stop nothing at all."""
    session_manager = _FakeSessionManager({"review-4124"})
    active = [_session("review-4124", 230)]
    owners = _failing_owners(
        pair_registry=None, job_supervisor=None,
        session_manager=session_manager, active_sessions=active,
    )

    with pytest.raises(ValidatedWorkCustodyUnproven, match="custody"):
        owners.terminate_every_session(230, "operator-terminated")

    assert session_manager.stopped == []


def test_terminate_every_session_ignores_other_issues(completion_intake) -> None:
    session_manager = _FakeSessionManager({"review-4124", "review-9999"})
    active = [_session("review-4124", 230), _session("review-9999", 231)]

    runtime_owners(
        completion_intake=completion_intake, pair_registry=None, job_supervisor=None,
        session_manager=session_manager, active_sessions=active,
    ).terminate_every_session(230, "operator-terminated")

    assert session_manager.stopped == ["review-4124"]


def test_terminate_every_session_reports_a_dead_terminal_as_cleared(
    completion_intake,
) -> None:
    """A terminal already gone was CLEARED, not killed.

    `killed_sessions` is rendered back to the operator, so reporting a dead
    `tech-lead-230` as killed made the HTTP result depend on terminal TYPE for
    identical input.
    """
    session_manager = _FakeSessionManager(set())  # nothing is running
    active = [_session("tech-lead-230", 230)]

    outcome = runtime_owners(
        completion_intake=completion_intake, pair_registry=None, job_supervisor=None,
        session_manager=session_manager, active_sessions=active,
    ).terminate_every_session(230, "operator-terminated")

    assert session_manager.stopped == []
    assert outcome.stopped_session_ids == ()
    assert outcome.cleared_session_ids == ("tech-lead-230",)
    assert outcome.settled_session_ids == ("tech-lead-230",)
    assert outcome.complete


def test_a_post_teardown_fault_is_reported_as_partial_not_deferred() -> None:
    """After `release_preserved`, "nothing was terminated" is a lie.

    The exchange pair is released, the publish retry abandoned and `issue-N`
    stopped by then, so a fault in the per-terminal loop -- including
    `ReconciliationRequired`, which the initial capture DOES re-raise -- must
    surface as a partial teardown rather than a deferral.
    """
    from issue_orchestrator.control.reconciliation import (
        ExternalSnapshot,
        ReconciliationRequired,
    )
    from issue_orchestrator.domain.validated_work_commands import (
        ValidatedWorkDispositionBatch,
    )

    owners = runtime_owners(
        completion_intake=None, pair_registry=None, job_supervisor=None,
        session_manager=_FakeSessionManager({"review-4124"}),
        active_sessions=[_session("review-4124", 230)],
    )
    owners.validated_work.dispose_at_termination.side_effect = [
        ValidatedWorkDispositionBatch.no_work(230, "issue-wide capture"),
        ReconciliationRequired(
            "issue", 230, ExternalSnapshot.for_issue(230, {"in-progress"}),
            ExternalSnapshot.for_issue(230, {"blocked"}),
        ),
    ]

    outcome = owners.terminate_every_session(230, "operator-terminated")

    assert not outcome.complete
    assert outcome.failures[0][0] == "review-4124"


def test_settled_ids_do_not_double_count_a_stopped_issue_terminal(
    completion_intake,
) -> None:
    """`cleared_active_session_ids` INCLUDES what was just stopped.

    Seeding `cleared` from it directly duplicated the ordinary `issue-N`
    terminal in `settled_session_ids`, the field whose whole purpose is to be
    the authoritative settled count. No prior test could see it: they all used
    `review-*`/`tech-lead-*` terminals, for which the overlap never forms.
    """
    session_manager = _FakeSessionManager({"issue-230"})
    active = [_session("issue-230", 230)]

    outcome = runtime_owners(
        completion_intake=completion_intake, pair_registry=None, job_supervisor=None,
        session_manager=session_manager, active_sessions=active,
    ).terminate_every_session(230, "operator-terminated")

    assert session_manager.stopped == ["issue-230"]
    assert outcome.stopped_session_ids == ("issue-230",)
    assert outcome.cleared_session_ids == ()
    assert outcome.settled_session_ids == ("issue-230",)


def test_a_dead_terminal_is_not_reported_as_maybe_running(completion_intake) -> None:
    """Liveness is checked BEFORE the preserve.

    A preserve fault on a terminal whose process is already gone used to land in
    `failures`, which the route renders as "may still be running" when nothing
    was running at all.
    """
    owners = runtime_owners(
        completion_intake=completion_intake, pair_registry=None, job_supervisor=None,
        session_manager=_FakeSessionManager(set()),  # nothing is running
        active_sessions=[_session("review-4124", 230)],
    )

    outcome = owners.terminate_every_session(230, "operator-terminated")

    assert outcome.complete
    assert outcome.failures == ()
    assert outcome.cleared_session_ids == ("review-4124",)
    # Exactly one capture: the issue-wide one. A dead terminal is never
    # preserved, so a preserve fault cannot be mistaken for a live terminal.
    assert owners.validated_work.dispose_at_termination.call_count == 1
