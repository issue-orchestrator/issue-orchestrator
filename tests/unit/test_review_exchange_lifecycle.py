"""Tests for issue-scoped runtime lifecycle boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.review_exchange_lifecycle import (
    terminate_issue_runtime,
)


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


def test_terminate_issue_runtime_stops_issue_rework_and_hidden_exchange() -> None:
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

    result = terminate_issue_runtime(
        issue_number=230,
        reason="reset-retry",
        pair_registry=pair_registry,
        job_supervisor=job_supervisor,
        session_manager=session_manager,
        active_sessions=active_sessions,
    )

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


def test_terminate_issue_runtime_clears_stale_active_session_records() -> None:
    session_manager = _FakeSessionManager(set())
    active_sessions = [_active_session("issue-230"), _active_session("issue-231")]

    result = terminate_issue_runtime(
        issue_number=230,
        reason="issue-completed",
        pair_registry=None,
        job_supervisor=None,
        session_manager=session_manager,
        active_sessions=active_sessions,
    )

    assert session_manager.stopped == []
    assert result.stopped_session_ids == ()
    assert result.cleared_active_session_ids == ("issue-230",)
    assert [session.terminal_id for session in active_sessions] == ["issue-231"]


def test_terminate_issue_runtime_requires_session_manager_for_active_records() -> None:
    pair_registry = Mock()

    with pytest.raises(RuntimeError, match="without a SessionManager"):
        terminate_issue_runtime(
            issue_number=230,
            reason="reset-retry",
            pair_registry=pair_registry,
            job_supervisor=None,
            session_manager=None,
            active_sessions=[_active_session("issue-230")],
        )

    pair_registry.release.assert_not_called()
