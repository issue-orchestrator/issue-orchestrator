"""Tests for the on-demand triage dispatch owner (control/triage_trigger.py)."""

from types import SimpleNamespace

from issue_orchestrator.control.triage_trigger import (
    HealthReviewResult,
    InvestigationResult,
    run_health_review,
    run_targeted_investigations,
)
from issue_orchestrator.domain.models import PendingTriageReview
from issue_orchestrator.domain.triage_session import TriageSessionFlavor


def _clock(values):
    """A ``now`` callable that yields the given values, holding the last."""
    seq = list(values)
    state = {"i": 0}

    def now() -> float:
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return float(seq[i])

    return now


def _issue(number=5980, *, labels=("blocked-failed",)):
    return SimpleNamespace(
        number=number,
        title=f"Issue {number}",
        body="body",
        milestone=None,
        labels=list(labels),
    )


class _Session:
    def __init__(self, stable_id: str) -> None:
        self.key = SimpleNamespace(stable_id=lambda: stable_id)
        self.terminal_id = stable_id.replace(":", "-")


class _State:
    def __init__(self) -> None:
        self.active_sessions: list[_Session] = []


class _FakeHost:
    """Minimal TriageDispatchHost fake: launch adds a session, tick drains it."""

    def __init__(self, *, issue, launch=True, ticks_to_complete=2) -> None:
        self.repository_host = SimpleNamespace(get_issue=lambda n: issue)
        self.state = _State()
        self._launch = launch
        self._ticks_to_complete = ticks_to_complete
        self.pause_calls = 0
        self.tick_count = 0
        self.launched: list = []
        self.killed: list[str] = []
        self._session: _Session | None = None

    def pause(self) -> None:
        self.pause_calls += 1

    def launch_triage_session(self, triage):
        self.launched.append(triage)
        if not self._launch:
            return None
        self._session = _Session(f"triage:{triage.issue_number}")
        self.state.active_sessions.append(self._session)
        return self._session

    def tick(self) -> bool:
        self.tick_count += 1
        if (
            self._session is not None
            and self.tick_count >= self._ticks_to_complete
            and self._session in self.state.active_sessions
        ):
            self.state.active_sessions.remove(self._session)
        return True

    def terminate_triage_session(self, session):
        # Faithful to the real facade (#6824 R7): terminate AND reconcile the
        # session out of active_sessions, returning a (clean) typed outcome.
        from issue_orchestrator.control.triage_trigger import TriageTerminationOutcome

        self.killed.append(session.terminal_id)
        self.state.active_sessions = [
            s for s in self.state.active_sessions if s.terminal_id != session.terminal_id
        ]
        return TriageTerminationOutcome()


def _noop_sleep(_seconds: float) -> None:
    pass


def test_happy_path_launches_and_drives_to_completion() -> None:
    host = _FakeHost(issue=_issue(5980), ticks_to_complete=2)
    results = run_targeted_investigations(
        host, [5980], now=_clock([0, 1, 2, 3, 4]), sleep=_noop_sleep
    )
    assert host.pause_calls == 1  # planner paused exactly once, up front
    assert len(host.launched) == 1
    triage = host.launched[0]
    assert triage.issue_number == 5980
    assert triage.flavor is TriageSessionFlavor.FAILURE_INVESTIGATION
    assert triage.failure is not None
    assert triage.failure.issue_number == 5980
    assert host.tick_count >= 2  # ticked until the session drained
    assert results == [
        InvestigationResult(
            5980, launched=True, completed=True,
            detail="investigation completed for issue #5980",
        )
    ]


def test_issue_not_found_is_not_launched() -> None:
    host = _FakeHost(issue=None)
    results = run_targeted_investigations(
        host, [4242], now=_clock([0]), sleep=_noop_sleep
    )
    assert host.pause_calls == 1  # pause is up-front, before per-issue work
    assert host.launched == []  # never attempted a launch
    assert results[0].launched is False
    assert results[0].completed is False
    assert "not found" in results[0].detail


def test_launch_declined_reports_not_launched() -> None:
    host = _FakeHost(issue=_issue(5980), launch=False)
    results = run_targeted_investigations(
        host, [5980], now=_clock([0]), sleep=_noop_sleep
    )
    assert len(host.launched) == 1  # attempted
    assert results[0].launched is False
    assert results[0].completed is False
    assert "launch failed" in results[0].detail
    assert host.tick_count == 0  # no drive loop when launch declined


def test_timeout_when_session_never_completes() -> None:
    # ticks_to_complete huge => the session never drains; the clock jumps past
    # the deadline so the drive loop gives up.
    host = _FakeHost(issue=_issue(5980), ticks_to_complete=10_000)
    # now() calls, in order: observed_at, deadline-calc, check#1, check#2
    results = run_targeted_investigations(
        host, [5980], now=_clock([0, 0, 0, 9_999]), sleep=_noop_sleep, timeout_s=100
    )
    assert results[0].launched is True
    assert results[0].completed is False
    assert "timed out" in results[0].detail
    # F7: the timed-out session is EXPLICITLY terminated (not left dangling for
    # close() to kill), and the result says so.
    assert "terminated" in results[0].detail
    assert host.killed == ["triage-5980"]
    assert host.state.active_sessions == []


def test_blocking_label_falls_back_to_manual_when_no_blocked_label() -> None:
    host = _FakeHost(issue=_issue(5980, labels=("agent:backend",)), ticks_to_complete=1)
    run_targeted_investigations(
        host, [5980], now=_clock([0, 1, 2]), sleep=_noop_sleep
    )
    assert host.launched[0].failure.blocking_label == "manual-triage"


def test_blocking_label_prefers_real_blocked_label() -> None:
    host = _FakeHost(
        issue=_issue(5980, labels=("agent:backend", "blocked-failed")),
        ticks_to_complete=1,
    )
    run_targeted_investigations(
        host, [5980], now=_clock([0, 1, 2]), sleep=_noop_sleep
    )
    assert host.launched[0].failure.blocking_label == "blocked-failed"


def _health_anchor(number: int = 200) -> PendingTriageReview:
    return PendingTriageReview(
        number,
        "Health Review — walk the floor",
        flavor=TriageSessionFlavor.HEALTH_REVIEW,
    )


class _FakeHealthHost:
    """Minimal TriageDispatchHost fake for the on-demand health-review driver.

    ``ensure_health_review_anchor`` returns a canned queued anchor (or None),
    ``launch_triage_session`` adds a session, and ``tick`` drains it.
    """

    def __init__(
        self, *, anchor=None, launch=True, ticks_to_complete=2
    ) -> None:
        self.state = _State()
        self._anchor = anchor
        self._launch = launch
        self._ticks_to_complete = ticks_to_complete
        self.pause_calls = 0
        self.ensure_calls = 0
        self.launched: list = []
        self.killed: list[str] = []
        self.tick_count = 0
        self._session: _Session | None = None

    def pause(self) -> None:
        self.pause_calls += 1

    def ensure_health_review_anchor(self):
        self.ensure_calls += 1
        return self._anchor

    def launch_triage_session(self, triage):
        self.launched.append(triage)
        if not self._launch:
            return None
        self._session = _Session(f"triage:{triage.issue_number}")
        self.state.active_sessions.append(self._session)
        return self._session

    def tick(self) -> bool:
        self.tick_count += 1
        if (
            self._session is not None
            and self.tick_count >= self._ticks_to_complete
            and self._session in self.state.active_sessions
        ):
            self.state.active_sessions.remove(self._session)
        return True

    def terminate_triage_session(self, session):
        # Faithful to the real facade (#6824 R7): terminate AND reconcile.
        from issue_orchestrator.control.triage_trigger import TriageTerminationOutcome

        self.killed.append(session.terminal_id)
        self.state.active_sessions = [
            s for s in self.state.active_sessions if s.terminal_id != session.terminal_id
        ]
        return TriageTerminationOutcome()


def test_health_review_launches_and_drives_to_completion() -> None:
    host = _FakeHealthHost(anchor=_health_anchor(200), ticks_to_complete=2)
    result = run_health_review(
        host, now=_clock([0, 1, 2, 3, 4]), sleep=_noop_sleep
    )
    assert host.pause_calls == 1  # planner paused up front, before ensuring
    assert host.ensure_calls == 1  # anchor ensured once, after the pause
    assert len(host.launched) == 1
    launched = host.launched[0]
    assert launched.flavor is TriageSessionFlavor.HEALTH_REVIEW
    assert launched.issue_number == 200
    assert host.tick_count >= 2  # ticked until the session drained
    assert result == HealthReviewResult(
        200, launched=True, completed=True,
        detail="health review completed for anchor #200",
    )


def test_health_review_not_launched_when_no_anchor() -> None:
    # ensure_health_review_anchor returns None (e.g. no triage agent configured).
    host = _FakeHealthHost(anchor=None)
    result = run_health_review(host, now=_clock([0]), sleep=_noop_sleep)
    assert host.pause_calls == 1  # pause is up-front, before ensuring the anchor
    assert host.launched == []  # never attempted a launch
    assert host.tick_count == 0
    assert result.anchor_issue_number is None
    assert result.launched is False
    assert result.completed is False
    assert "no health-review anchor" in result.detail


def test_health_review_launch_declined_reports_not_launched() -> None:
    host = _FakeHealthHost(anchor=_health_anchor(200), launch=False)
    result = run_health_review(host, now=_clock([0]), sleep=_noop_sleep)
    assert len(host.launched) == 1  # attempted
    assert result.anchor_issue_number == 200
    assert result.launched is False
    assert result.completed is False
    assert "launch failed" in result.detail
    assert host.tick_count == 0  # no drive loop when launch declined


def test_health_review_times_out_when_session_never_completes() -> None:
    host = _FakeHealthHost(anchor=_health_anchor(200), ticks_to_complete=10_000)
    # now() calls, in order: ensure, deadline-calc, check#1, check#2
    result = run_health_review(
        host, now=_clock([0, 0, 0, 9_999]), sleep=_noop_sleep, timeout_s=100
    )
    assert result.anchor_issue_number == 200
    assert result.launched is True
    assert result.completed is False
    assert "timed out" in result.detail
    # F7: the timed-out session is explicitly terminated.
    assert "terminated" in result.detail
    assert host.killed == ["triage-200"]
    assert host.state.active_sessions == []
