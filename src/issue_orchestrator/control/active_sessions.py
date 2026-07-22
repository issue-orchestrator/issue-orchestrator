"""Helpers for active session tracking."""

import logging
from typing import Iterable

from ..domain.models import Session

logger = logging.getLogger(__name__)


def append_unique_active_sessions(
    active_sessions: list[Session],
    incoming: list[Session],
) -> list[Session]:
    """Append sessions while preserving unique terminal identity.

    Returns the sessions that were actually added, so callers do not need to
    re-derive reporting state from the mutated active-session list.
    """
    existing_ids = {s.terminal_id for s in active_sessions}
    added: list[Session] = []
    for session in incoming:
        if session.terminal_id in existing_ids:
            logger.warning(
                "[ACTIVE_SESSIONS] Duplicate terminal suppressed: %s (issue=%s)",
                session.terminal_id,
                session.issue.number,
            )
            continue
        active_sessions.append(session)
        existing_ids.add(session.terminal_id)
        added.append(session)
    return added


def has_active_terminal(active_sessions: list[Session], terminal_id: str) -> bool:
    """Return whether a terminal id is still active in the live session list."""
    return any(session.terminal_id == terminal_id for session in active_sessions)


def active_session_run_id(
    active_sessions: Iterable[Session], issue_number: int
) -> str | None:
    """Run id of the issue's live session, or None when none runs (#6779 R1).

    ``terminal_id`` is deterministic per issue (``issue-<n>``) so it cannot
    tell one generation from its replacement; ``run_assets.run_id`` is the
    per-launch identity that can. The tech_lead kill lifecycle binds approval to
    this id at proposal time and re-checks it at execution: the single owner
    both the capture (completion planning) and execution (kill executor)
    sides read through.
    """
    for session in active_sessions:
        if session.issue.number == issue_number:
            return session.run_assets.run_id
    return None
