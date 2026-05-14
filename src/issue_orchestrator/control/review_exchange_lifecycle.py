"""Issue-scoped runtime lifecycle helpers.

This module is the behavior owner for issue terminal boundaries that must
tear down hidden review-exchange work and, when requested, visible issue/rework
terminal sessions. Call sites should use these helpers instead of directly
reaching into pair registries, background job supervisors, or session managers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

from .completion_review_exchange import is_review_exchange_job_for_issue

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..ports.persistent_exchange_pair_registry import (
        PersistentExchangePairRegistry,
    )
    from .background_job_supervisor import BackgroundJobSupervisor
    from .session_manager import SessionManager, SessionRef

from .session_manager import SessionType

logger = logging.getLogger(__name__)


ISSUE_RUNTIME_SESSION_TYPES = (SessionType.ISSUE, SessionType.REWORK)


@dataclass(frozen=True)
class ReviewExchangeCancellation:
    """Result of cancelling review-exchange work for one issue."""

    issue_number: int
    cancelled_job_ids: tuple[str, ...]


@dataclass(frozen=True)
class IssueRuntimeTermination:
    """Result of applying an issue-scoped runtime lifecycle boundary."""

    issue_number: int
    review_exchange: ReviewExchangeCancellation
    stopped_session_ids: tuple[str, ...]
    cleared_active_session_ids: tuple[str, ...]

    @property
    def cancelled_job_ids(self) -> tuple[str, ...]:
        return self.review_exchange.cancelled_job_ids


def cancel_issue_review_exchange(
    *,
    issue_number: int,
    reason: str,
    pair_registry: "PersistentExchangePairRegistry | None",
    job_supervisor: "BackgroundJobSupervisor | None",
) -> ReviewExchangeCancellation:
    """Terminate persistent pair resources and stop supervised polling.

    Review exchange work has two owners: the pair registry owns the persistent
    coder/reviewer processes, and the background supervisor owns the async job
    status observed by the main tick. Operator cancellation must touch both or
    the visible issue session can stop while a hidden exchange continues to
    report "still running".
    """
    if pair_registry is not None:
        pair_registry.release(issue_number, reason=reason)

    cancelled: tuple[str, ...] = ()
    if job_supervisor is not None:
        cancelled = tuple(
            job_supervisor.cancel_matching(
                lambda job_id: is_review_exchange_job_for_issue(job_id, issue_number),
                reason=reason,
            )
        )
    if pair_registry is not None or cancelled:
        logger.info(
            "[REVIEW_EXCHANGE] cancelled issue=%d reason=%s jobs=%s",
            issue_number,
            reason,
            ",".join(cancelled) if cancelled else "none",
        )
    return ReviewExchangeCancellation(
        issue_number=issue_number,
        cancelled_job_ids=cancelled,
    )


def terminate_issue_runtime(
    *,
    issue_number: int,
    reason: str,
    pair_registry: "PersistentExchangePairRegistry | None",
    job_supervisor: "BackgroundJobSupervisor | None",
    session_manager: "SessionManager | None" = None,
    active_sessions: list["Session"] | None = None,
    session_types: Iterable[SessionType] = ISSUE_RUNTIME_SESSION_TYPES,
) -> IssueRuntimeTermination:
    """Apply an issue terminal boundary to every issue-scoped runtime owner.

    The review exchange pair/job is always checked. Visible ``issue-N`` and
    ``rework-N`` sessions are stopped when a ``SessionManager`` is supplied.
    If an active-session registry is supplied, stale records for already-gone
    issue/rework sessions are cleared in the same boundary so queue eligibility
    does not remain blocked by a dead terminal.
    """
    refs = tuple(_issue_runtime_session_refs(issue_number, session_types))
    active_names = _active_session_names(active_sessions)
    matching_active = active_names.intersection(ref.name for ref in refs)
    if session_manager is None and matching_active:
        raise RuntimeError(
            "cannot terminate active issue runtime sessions without a "
            f"SessionManager: issue={issue_number} sessions={sorted(matching_active)}"
        )

    review_exchange = cancel_issue_review_exchange(
        issue_number=issue_number,
        reason=reason,
        pair_registry=pair_registry,
        job_supervisor=job_supervisor,
    )

    stopped: list[str] = []
    stale: list[str] = []
    if session_manager is not None:
        for ref in refs:
            if session_manager.exists(ref):
                session_manager.stop(ref)
                stopped.append(ref.name)
            elif ref.name in matching_active:
                stale.append(ref.name)

    terminal_ids_to_clear = _active_session_ids_to_clear(
        active_sessions,
        set(stopped).union(stale),
    )
    _drop_active_session_records(active_sessions, terminal_ids_to_clear)
    if stopped or terminal_ids_to_clear:
        logger.info(
            "[ISSUE_RUNTIME] terminated issue=%d reason=%s stopped=%s cleared=%s",
            issue_number,
            reason,
            ",".join(stopped) if stopped else "none",
            ",".join(terminal_ids_to_clear) if terminal_ids_to_clear else "none",
        )
    return IssueRuntimeTermination(
        issue_number=issue_number,
        review_exchange=review_exchange,
        stopped_session_ids=tuple(stopped),
        cleared_active_session_ids=terminal_ids_to_clear,
    )


def _issue_runtime_session_refs(
    issue_number: int,
    session_types: Iterable[SessionType],
) -> list["SessionRef"]:
    from .session_manager import SessionRef

    return [
        SessionRef(session_type=session_type, number=issue_number)
        for session_type in session_types
        if session_type in ISSUE_RUNTIME_SESSION_TYPES
    ]


def _active_session_names(active_sessions: list["Session"] | None) -> set[str]:
    if active_sessions is None:
        return set()
    return {session.terminal_id for session in active_sessions}


def _active_session_ids_to_clear(
    active_sessions: list["Session"] | None,
    terminal_ids: set[str],
) -> tuple[str, ...]:
    if active_sessions is None or not terminal_ids:
        return ()
    return tuple(
        session.terminal_id for session in active_sessions
        if session.terminal_id in terminal_ids
    )


def _drop_active_session_records(
    active_sessions: list["Session"] | None,
    terminal_ids: tuple[str, ...],
) -> None:
    if active_sessions is None or not terminal_ids:
        return
    terminal_id_set = set(terminal_ids)
    active_sessions[:] = [
        session for session in active_sessions
        if session.terminal_id not in terminal_id_set
    ]
