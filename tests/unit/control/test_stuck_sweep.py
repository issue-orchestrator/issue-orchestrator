"""Tech-lead attention sweep for terminally-stuck issues (#6823).

These tests drive the real ``stuck_sweep`` policy owner and its ``fact_gatherer``
wiring through fake ports (a recording repository host and a real
``LabelManager``), covering: the pure timer/config due-gate (zero GitHub calls
when not due), the scan/injection that re-injects a stuck issue as a
``timed_out`` recovered failure, the done guard, dedup/ownership, the bounded
recovery counter + exhaustion escalation, machinery/human exclusions, config
parsing, and the end-to-end wiring into the reactive-triage reaction model.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.stuck_sweep import (
    run_stuck_sweep,
    stuck_sweep_due,
)
from issue_orchestrator.control.triage_reaction import TriageReactionPolicy
from issue_orchestrator.domain.models import Issue, OrchestratorState
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_sections import parse_triage_config
from issue_orchestrator.ports.event_sink import InMemoryEventSink


def _config(
    *,
    enabled: bool = True,
    interval_minutes: int = 15,
    max_recovery_attempts: int = 3,
    triage_agent: str | None = "agent:triage",
    on_failure: bool = True,
    filter_label: str | None = None,
) -> Config:
    config = Config()
    config.triage_review_agent = triage_agent
    config.triage_review_on_failure = on_failure
    config.filtering.label = filter_label
    config.triage.stuck_sweep.enabled = enabled
    config.triage.stuck_sweep.interval_minutes = interval_minutes
    config.triage.stuck_sweep.max_recovery_attempts = max_recovery_attempts
    return config


class _RecordingHost:
    """RepositoryHost fake honoring GitHub's label AND-filter and state."""

    def __init__(self, issues) -> None:
        self._issues = list(issues)
        self.calls: list[dict] = []

    def list_issues(self, labels=None, state="open", limit=100, **kwargs):
        self.calls.append(
            {"labels": list(labels or []), "state": state, "limit": limit}
        )
        wanted = {name.casefold() for name in (labels or [])}
        matched = [
            issue
            for issue in self._issues
            if state == "all" or issue.state == state
            if wanted <= {name.casefold() for name in issue.labels}
        ]
        return matched[:limit]

    def get_prs_with_label(self, *args, **kwargs):
        return []


def _issue(number: int, *, labels, state: str = "open", title: str | None = None):
    return Issue(
        number=number,
        title=title or f"Issue {number}",
        labels=list(labels),
        state=state,
        repo="test/repo",
        body="body",
        milestone="M1",
    )


# ---------------------------------------------------------------------------
# Due gate (pure state/config math, zero GitHub calls)
# ---------------------------------------------------------------------------


def test_due_false_when_disabled():
    config = _config(enabled=False)
    assert stuck_sweep_due(config, OrchestratorState(), now=10_000.0) is False


def test_due_false_when_not_yet_elapsed():
    config = _config(interval_minutes=15)
    state = OrchestratorState()
    state.last_stuck_sweep_at = 10_000.0
    # 10 minutes later — under the 15-minute interval.
    assert stuck_sweep_due(config, state, now=10_000.0 + 600) is False


def test_due_true_when_elapsed():
    config = _config(interval_minutes=15)
    state = OrchestratorState()
    state.last_stuck_sweep_at = 10_000.0
    assert stuck_sweep_due(config, state, now=10_000.0 + 15 * 60) is True


def test_due_false_without_triage_agent():
    config = _config(triage_agent=None)
    assert stuck_sweep_due(config, OrchestratorState(), now=10_000.0) is False


def test_due_false_without_triage_on_failure():
    config = _config(on_failure=False)
    assert stuck_sweep_due(config, OrchestratorState(), now=10_000.0) is False


def test_not_due_sweep_makes_zero_github_calls():
    """A configured-but-not-due sweep must not touch the network."""
    config = _config(interval_minutes=60)
    state = OrchestratorState()
    # create_snapshot uses real time.time(); a just-now sweep is not yet due.
    state.last_stuck_sweep_at = time.time()
    host = _RecordingHost([_issue(1, labels=["blocked-failed"])])
    gatherer = FactGatherer(config=config, repository_host=host)
    gatherer.create_snapshot(state, issues=[])
    assert host.calls == []
    assert state.discovered_failures == []


# ---------------------------------------------------------------------------
# Scan + injection
# ---------------------------------------------------------------------------


def test_scan_injects_recovered_failure_as_timed_out():
    config = _config()
    state = OrchestratorState()
    host = _RecordingHost([_issue(7, labels=["agent:web", "blocked-failed"])])
    result = run_stuck_sweep(
        config, state, host, LabelManager(config), now=50_000.0
    )
    assert len(result.recovered) == 1
    failure = result.recovered[0]
    # CRITICAL: timed_out (never blocked) so the reaction model INVESTIGATES.
    assert failure.failure_reason == "timed_out"
    # The real terminal label rides along for context.
    assert failure.blocking_label == "blocked-failed"
    assert failure.issue_number == 7
    assert failure.observed_at == 50_000.0
    # Recovery counter incremented and no exhaustion.
    assert state.recovery_attempts == {7: 1}
    assert result.exhausted == ()


def test_scan_prefers_blocked_failed_label_when_multiple():
    config = _config()
    state = OrchestratorState()
    host = _RecordingHost(
        [_issue(8, labels=["blocked", "blocked-failed", "blocked-cross-milestone"])]
    )
    result = run_stuck_sweep(
        config, state, host, LabelManager(config), now=1.0
    )
    assert result.recovered[0].blocking_label == "blocked-failed"


def test_scan_scopes_query_to_filter_label():
    config = _config(filter_label="agent:web")
    state = OrchestratorState()
    host = _RecordingHost([_issue(9, labels=["agent:web", "blocked"])])
    run_stuck_sweep(config, state, host, LabelManager(config), now=1.0)
    assert host.calls[0]["labels"] == ["agent:web"]
    assert host.calls[0]["state"] == "open"


# ---------------------------------------------------------------------------
# Done guard
# ---------------------------------------------------------------------------


def test_done_guard_skips_closed_issue():
    config = _config()
    state = OrchestratorState()
    # Closed issue that still (stale) carries a blocking label.
    host = _RecordingHost(
        [_issue(1, labels=["blocked-failed"], state="closed")]
    )
    result = run_stuck_sweep(config, state, host, LabelManager(config), now=1.0)
    assert result.recovered == ()
    assert state.recovery_attempts == {}


def test_done_guard_skips_issue_without_blocking_label():
    config = _config()
    state = OrchestratorState()
    host = _RecordingHost([_issue(1, labels=["agent:web", "in-progress"])])
    result = run_stuck_sweep(config, state, host, LabelManager(config), now=1.0)
    assert result.recovered == ()


def test_excludes_human_and_machinery_labels():
    config = _config()
    state = OrchestratorState()
    labels = LabelManager(config)
    host = _RecordingHost(
        [
            _issue(1, labels=[labels.needs_human]),
            _issue(2, labels=[labels.triage_needs_human]),
            _issue(3, labels=[labels.provider_unavailable]),
            _issue(4, labels=["proposed-triage"]),
            _issue(5, labels=["triage-observation"]),
        ]
    )
    result = run_stuck_sweep(config, state, host, labels, now=1.0)
    assert result.recovered == ()
    assert state.recovery_attempts == {}


# ---------------------------------------------------------------------------
# Dedup / ownership
# ---------------------------------------------------------------------------


def test_dedup_skips_active_pending_cohort_and_discovered():
    config = _config()
    state = OrchestratorState()
    # 11 is in an active session; 12 is a pending triage review; 13 is a cohort
    # member of a pending storm review; 14 was discovered this tick.
    state.active_sessions = [SimpleNamespace(issue=SimpleNamespace(number=11))]
    state.pending_triage_reviews = [
        SimpleNamespace(issue_number=12, problem_cohort=()),
        SimpleNamespace(
            issue_number=99,
            problem_cohort=(SimpleNamespace(issue_number=13),),
        ),
    ]
    state.discovered_failures = [SimpleNamespace(issue_number=14)]
    host = _RecordingHost(
        [
            _issue(11, labels=["blocked-failed"]),
            _issue(12, labels=["blocked-failed"]),
            _issue(13, labels=["blocked-failed"]),
            _issue(14, labels=["blocked-failed"]),
            _issue(15, labels=["blocked-failed"]),
        ]
    )
    result = run_stuck_sweep(config, state, host, LabelManager(config), now=1.0)
    recovered_numbers = {failure.issue_number for failure in result.recovered}
    assert recovered_numbers == {15}


# ---------------------------------------------------------------------------
# Bounded attempts / escalation
# ---------------------------------------------------------------------------


def test_over_limit_issue_not_injected_and_reported_exhausted():
    config = _config(max_recovery_attempts=3)
    state = OrchestratorState()
    state.recovery_attempts = {5: 3}  # already at the limit
    host = _RecordingHost([_issue(5, labels=["blocked-failed"])])
    result = run_stuck_sweep(config, state, host, LabelManager(config), now=1.0)
    assert result.recovered == ()
    assert result.exhausted == (5,)
    # Counter is NOT incremented past the limit (no unbounded growth).
    assert state.recovery_attempts == {5: 3}


def test_attempt_counter_increments_across_recoveries():
    config = _config(max_recovery_attempts=3)
    state = OrchestratorState()
    state.recovery_attempts = {6: 1}
    host = _RecordingHost([_issue(6, labels=["blocked-failed"])])
    result = run_stuck_sweep(config, state, host, LabelManager(config), now=1.0)
    assert result.recovered[0].issue_number == 6
    assert state.recovery_attempts == {6: 2}


def test_empty_scan_returns_empty_result():
    config = _config()
    state = OrchestratorState()
    host = _RecordingHost([])
    result = run_stuck_sweep(config, state, host, LabelManager(config), now=1.0)
    assert result.recovered == ()
    assert result.exhausted == ()


# ---------------------------------------------------------------------------
# fact_gatherer wiring -> reactive-triage reaction
# ---------------------------------------------------------------------------


def test_due_sweep_populates_snapshot_and_reaction_investigates():
    config = _config()
    state = OrchestratorState()  # last_stuck_sweep_at defaults to 0.0 -> due
    host = _RecordingHost([_issue(21, labels=["agent:web", "blocked-failed"])])
    events = InMemoryEventSink()
    gatherer = FactGatherer(config=config, repository_host=host, events=events)

    snapshot = gatherer.create_snapshot(state, issues=[])

    # The recovered failure is in THIS tick's snapshot (reorder guarantee).
    numbers = {failure.issue_number for failure in snapshot.discovered_failures}
    assert 21 in numbers
    assert state.last_stuck_sweep_at > 0.0

    # The reaction model classifies the timed_out failure as INVESTIGATE.
    reaction = TriageReactionPolicy(
        config=config,
        labels=LabelManager(config),
        dependency_evaluator=None,
        clock=lambda: 1.0,
    ).assess(snapshot)
    assert 21 in {p.issue_number for p in reaction.investigations}

    # An observation event was emitted for the sweep.
    names = {event.name for event in events.events}
    assert "triage.stuck_sweep" in names


def test_disabled_sweep_injects_nothing_via_fact_gatherer():
    config = _config(enabled=False)
    state = OrchestratorState()
    host = _RecordingHost([_issue(30, labels=["blocked-failed"])])
    gatherer = FactGatherer(config=config, repository_host=host)
    gatherer.create_snapshot(state, issues=[])
    assert state.discovered_failures == []
    assert host.calls == []


# ---------------------------------------------------------------------------
# Config parse / defaults
# ---------------------------------------------------------------------------


def test_stuck_sweep_config_defaults():
    triage = parse_triage_config({})
    assert triage.stuck_sweep.enabled is False
    assert triage.stuck_sweep.interval_minutes == 15
    assert triage.stuck_sweep.max_recovery_attempts == 3


def test_stuck_sweep_config_parsed_from_yaml_dict():
    triage = parse_triage_config(
        {
            "stuck_sweep": {
                "enabled": True,
                "interval_minutes": 30,
                "max_recovery_attempts": 5,
            }
        }
    )
    assert triage.stuck_sweep.enabled is True
    assert triage.stuck_sweep.interval_minutes == 30
    assert triage.stuck_sweep.max_recovery_attempts == 5
    assert triage.to_event_dict()["stuck_sweep"] == {
        "enabled": True,
        "interval_minutes": 30,
        "max_recovery_attempts": 5,
    }
