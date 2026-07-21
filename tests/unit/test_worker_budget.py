"""Unit tests for the worker-slot accounting owner.

This is the single owner both the planner's ``_launch_budgets`` and the
orchestrator's E2E start-gate consult, so these tests pin the rule directly:
the tech lead's reserved additive sessions are excluded from the worker budget
when ``triage.max_concurrent`` is set, otherwise every active session counts.
"""

from issue_orchestrator.control.worker_budget import (
    active_triage_session_count,
    active_worker_session_count,
    worker_slot_free,
)

from tests.unit.test_planner import make_config, make_issue, make_session


def _triage_session(number: int, agent_label: str):
    session = make_session(make_issue(number, labels=[agent_label]))
    session.agent_label = agent_label
    return session


class TestActiveWorkerSessionCount:
    def test_shared_budget_counts_every_session(self):
        """Default (triage.max_concurrent unset): all active sessions count."""
        config = make_config(triage_review_agent="agent:triage")
        assert config.triage.max_concurrent is None
        sessions = [
            make_session(make_issue(1)),
            _triage_session(2, "agent:triage"),
        ]
        assert active_worker_session_count(config, sessions) == 2

    def test_reserved_budget_excludes_triage_sessions(self):
        """Reserved additive budget: triage sessions are NOT charged to the
        worker budget."""
        config = make_config(triage_review_agent="agent:triage")
        config.triage.max_concurrent = 1
        sessions = [
            make_session(make_issue(1)),
            _triage_session(2, "agent:triage"),
        ]
        assert active_triage_session_count(config, sessions) == 1
        assert active_worker_session_count(config, sessions) == 1

    def test_empty_is_zero(self):
        config = make_config()
        assert active_worker_session_count(config, []) == 0


class TestWorkerSlotFree:
    def test_free_when_below_max(self):
        config = make_config(max_concurrent_sessions=2)
        assert worker_slot_free(config, [make_session(make_issue(1))]) is True

    def test_not_free_when_workers_saturate(self):
        config = make_config(max_concurrent_sessions=1)
        assert worker_slot_free(config, [make_session(make_issue(1))]) is False

    def test_reserved_triage_session_leaves_worker_slot_free(self):
        """A tech-lead session on the reserved budget does not consume the
        worker slot the E2E start-gate competes for."""
        config = make_config(
            triage_review_agent="agent:triage", max_concurrent_sessions=1
        )
        config.triage.max_concurrent = 1
        assert worker_slot_free(config, [_triage_session(9, "agent:triage")]) is True

    def test_shared_triage_session_consumes_worker_slot(self):
        """Default: a triage session shares the worker budget, so it occupies
        the only worker slot - unchanged behavior."""
        config = make_config(
            triage_review_agent="agent:triage", max_concurrent_sessions=1
        )
        assert worker_slot_free(config, [_triage_session(9, "agent:triage")]) is False
