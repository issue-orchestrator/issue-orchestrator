"""Worker-slot accounting — the single owner of "which active sessions count
against the worker budget (``max_concurrent_sessions``)".

Two seams must agree on this rule or they drift (cross-path rule drift):

* the planner's ``_launch_budgets`` computes remaining worker capacity, and
* the orchestrator's E2E start-gate asks "is a worker slot free?" before it
  lets a first-class E2E run claim one.

The rule: the triage tech lead draws from its own reserved additive budget
when ``triage.max_concurrent`` is set, so its active sessions are NOT charged
to the worker budget; otherwise (the shared-budget default) every active
session counts. E2E, by contrast, is a WORKER workload — it is accounted here,
against ``max_concurrent_sessions``, never against the triage reserved slot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from .triage_session_policy import is_triage_session

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..infra.config import Config


def active_triage_session_count(
    config: "Config", active_sessions: "Sequence[Session]"
) -> int:
    """Number of active sessions launched under the configured triage agent.

    Triage identity is the ADR-0031 owner rule (agent label == the configured
    ``triage_review_agent``); both triage variants launch as ``issue-{N}``
    sessions under that agent, so the agent label is what distinguishes them.
    """
    return sum(
        1
        for session in active_sessions
        if is_triage_session(config.triage_review_agent, session.agent_label)
    )


def active_worker_session_count(
    config: "Config", active_sessions: "Sequence[Session]"
) -> int:
    """Active sessions charged against ``max_concurrent_sessions``.

    Equals ``len(active_sessions)`` in the shared-budget default (unchanged);
    with a reserved triage budget the tech-lead sessions are additive and
    excluded so they never steal worker slots.
    """
    if config.triage.max_concurrent is None:
        return len(active_sessions)
    return len(active_sessions) - active_triage_session_count(config, active_sessions)


def worker_slot_free(
    config: "Config", active_sessions: "Sequence[Session]"
) -> bool:
    """Whether at least one worker slot is unoccupied right now.

    Uses the SAME accounting as the planner so the E2E start-gate competes for
    the worker budget, not the tech-lead's reserved slot.
    """
    return (
        active_worker_session_count(config, active_sessions)
        < config.max_concurrent_sessions
    )
