"""#7297: a GitHub rate limit defers launches; it never spends a retry or escalates.

Covers the owner chain below the launcher: the shared window, the launch gate
every launch path passes, and the planner's refusal to launch while the window
is open. The real-launcher incident reproduction lives with the other tech-lead
launch tests in ``test_session_launcher.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from issue_orchestrator.adapters.github.rate_limit import github_http_failure
from issue_orchestrator.control.actions import ActionType
from issue_orchestrator.control.host_rate_limit_launch_gate import (
    RATE_LIMIT_DEFER_REASON,
    HostRateLimitLaunchGate,
)
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.session_launch_types import (
    LaunchDisposition,
    LaunchResult,
)
from issue_orchestrator.domain.host_rate_limit import (
    RATE_LIMIT_DEFERRAL_BOUND,
    HostRateLimit,
    HostRateLimitWindow,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.ports.event_sink import InMemoryEventSink
from tests.unit.test_planner import make_config, make_issue, make_snapshot

T0 = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _limit(resets_at: datetime, kind: str = "primary") -> HostRateLimit:
    return HostRateLimit(resets_at=resets_at, kind=kind, resource="search")  # type: ignore[arg-type]


def _rate_limited_error(resets_at: datetime) -> Exception:
    """The incident error, built by the adapter's own failure constructor."""
    return github_http_failure(
        "GitHub GET /search/issues failed: 403 — API rate limit exceeded",
        status_code=403,
        headers=httpx.Headers({
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(int(resets_at.timestamp())),
            "x-ratelimit-resource": "search",
        }),
        response_text='{"message": "API rate limit exceeded for installation"}',
        method="GET",
        url="/search/issues",
        now=lambda: T0,
    )


def _deferral_events(events: InMemoryEventSink) -> list[dict]:
    return [
        event.data
        for event in events.events
        if event.name == EventName.SESSION_LAUNCH_DEFERRED_RATE_LIMIT
    ]


class TestHostRateLimitWindow:
    def test_open_until_the_reset_then_closed(self) -> None:
        window = HostRateLimitWindow()
        window.observe(_limit(T0 + timedelta(minutes=5)), T0)

        held = window.open_at(T0 + timedelta(minutes=4))
        assert held is not None and held.limit.resets_at == T0 + timedelta(minutes=5)
        assert window.open_at(T0 + timedelta(minutes=5)) is None

    def test_back_to_back_refusals_are_one_episode_and_hit_the_bound(self) -> None:
        window = HostRateLimitWindow()
        now = T0
        episode = window.observe(_limit(now + timedelta(hours=1)), now)
        while not episode.bound_exceeded:
            # Re-attempted just after each reset, still refused.
            now = episode.limit.resets_at + timedelta(minutes=1)
            episode = window.observe(_limit(now + timedelta(hours=1)), now)

        assert episode.limited_since == T0
        assert episode.limited_for >= RATE_LIMIT_DEFERRAL_BOUND

    def test_a_late_tick_does_not_restart_the_episode(self) -> None:
        """Codex r3: only positive recovery ends an episode, never elapsed time."""
        window = HostRateLimitWindow()
        now = T0
        episode = window.observe(_limit(now + timedelta(hours=1)), now)
        while not episode.bound_exceeded:
            # Each refusal lands eleven minutes after the previous reset.
            now = episode.limit.resets_at + timedelta(minutes=11)
            episode = window.observe(_limit(now + timedelta(hours=1)), now)

        assert episode.limited_since == T0

    def test_recovery_starts_the_next_episode_afresh(self) -> None:
        window = HostRateLimitWindow()
        window.observe(_limit(T0 + timedelta(hours=1)), T0)
        window.recovered()
        later = T0 + timedelta(hours=3)

        episode = window.observe(_limit(later + timedelta(minutes=1)), later)

        assert episode.limited_since == later
        assert not episode.bound_exceeded

    def test_an_earlier_reset_never_shortens_the_window(self) -> None:
        window = HostRateLimitWindow()
        window.observe(_limit(T0 + timedelta(hours=1)), T0)

        episode = window.observe(_limit(T0 + timedelta(minutes=2), "secondary"), T0)

        assert episode.limit.resets_at == T0 + timedelta(hours=1)

    def test_naive_reset_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            HostRateLimit(resets_at=datetime(2026, 9, 25), kind="primary")


class TestHostRateLimitLaunchGate:
    def _gate(self, clock: _Clock) -> tuple[HostRateLimitLaunchGate, InMemoryEventSink]:
        events = InMemoryEventSink()
        return HostRateLimitLaunchGate(HostRateLimitWindow(), events, clock), events

    def test_escaped_rate_limit_becomes_a_deferral_and_opens_the_window(self) -> None:
        clock = _Clock(T0)
        gate, events = self._gate(clock)
        resets = T0 + timedelta(minutes=10)

        def attempt() -> LaunchResult:
            raise _rate_limited_error(resets)

        result = gate.launch(attempt, issue_number=7297, work="review")

        assert result.disposition is LaunchDisposition.HOST_RATE_LIMITED
        assert result.host_rate_limit is not None
        assert result.host_rate_limit.resets_at == resets
        held = gate.window.open_at(T0)
        assert held is not None and held.limit.resets_at == resets
        (event,) = _deferral_events(events)
        assert event["issue_number"] == 7297
        assert event["resets_at"] == resets.isoformat()
        assert event["resource"] == "search"
        assert event["attempted"] is True
        assert event["retry_budget_spent"] is False

    def test_a_wrapped_rate_limit_is_still_a_rate_limit(self) -> None:
        clock = _Clock(T0)
        gate, _ = self._gate(clock)

        def attempt() -> LaunchResult:
            try:
                raise _rate_limited_error(T0 + timedelta(minutes=1))
            except Exception as exc:
                raise RuntimeError("board snapshot failed") from exc

        result = gate.launch(attempt, issue_number=1, work="tech_lead")

        assert result.disposition is LaunchDisposition.HOST_RATE_LIMITED

    def test_other_errors_still_propagate(self) -> None:
        gate, events = self._gate(_Clock(T0))

        def attempt() -> LaunchResult:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            gate.launch(attempt, issue_number=1, work="review")
        assert _deferral_events(events) == []

    def test_open_window_is_not_attempted(self) -> None:
        clock = _Clock(T0)
        gate, events = self._gate(clock)
        gate.window.observe(_limit(T0 + timedelta(minutes=10)), T0)
        attempts: list[int] = []

        def attempt() -> LaunchResult:
            attempts.append(1)
            return LaunchResult(None, True)

        clock.now = T0 + timedelta(minutes=9)
        result = gate.launch(attempt, issue_number=5, work="rework")

        assert attempts == []
        assert result.disposition is LaunchDisposition.HOST_RATE_LIMITED
        (event,) = _deferral_events(events)
        assert event["attempted"] is False

    def test_returned_rate_limit_from_a_catching_launch_opens_the_window(self) -> None:
        """Tech-lead prep catches its own error and RETURNS the classification."""
        gate, _ = self._gate(_Clock(T0))
        resets = T0 + timedelta(minutes=3)
        returned = LaunchResult.input_preparation_failed(
            "Tech Lead session data preparation failed", _rate_limited_error(resets)
        )

        result = gate.launch(lambda: returned, issue_number=1, work="tech_lead")

        assert result.disposition is LaunchDisposition.HOST_RATE_LIMITED
        assert gate.window.open_at(T0) is not None

    def test_a_limit_that_never_lifts_is_handed_back_as_a_retryable_failure(self) -> None:
        """Bounded: a permanently rate-limited token still reaches a human."""
        clock = _Clock(T0)
        gate, events = self._gate(clock)
        # Refused hourly since T0, re-attempted a minute after each reset.
        now = T0
        while now - T0 < RATE_LIMIT_DEFERRAL_BOUND:
            gate.window.observe(_limit(now + timedelta(hours=1)), now)
            now += timedelta(hours=1, minutes=1)
        clock.now = now

        def attempt() -> LaunchResult:
            raise _rate_limited_error(clock.now + timedelta(hours=1))

        result = gate.launch(attempt, issue_number=9, work="tech_lead")

        assert result.disposition is LaunchDisposition.RETRYABLE_FAILURE
        assert result.host_rate_limit is None
        assert "deferral bound" in result.reason
        assert _deferral_events(events)[-1]["retry_budget_spent"] is True

    def test_success_passes_through_untouched(self) -> None:
        gate, events = self._gate(_Clock(T0))
        launched = LaunchResult(None, True)

        assert gate.launch(lambda: launched, issue_number=1, work="issue") is launched
        assert _deferral_events(events) == []

    def test_a_launch_that_gets_through_ends_the_episode(self) -> None:
        clock = _Clock(T0)
        gate, _ = self._gate(clock)
        gate.window.observe(_limit(T0 + timedelta(minutes=1)), T0)
        clock.now = T0 + timedelta(hours=5)

        gate.launch(lambda: LaunchResult(None, True), issue_number=1, work="issue")

        def refused() -> LaunchResult:
            raise _rate_limited_error(clock.now + timedelta(minutes=1))

        result = gate.launch(refused, issue_number=1, work="issue")
        assert result.disposition is LaunchDisposition.HOST_RATE_LIMITED, (
            "a fresh episode after recovery must defer, not hit the old bound"
        )


class TestLaunchResultInvariant:
    def test_rate_limited_disposition_requires_the_typed_limit(self) -> None:
        with pytest.raises(ValueError):
            LaunchResult(None, False, "x", disposition=LaunchDisposition.HOST_RATE_LIMITED)

    def test_input_failure_without_a_rate_limit_is_retryable(self) -> None:
        result = LaunchResult.input_preparation_failed("prep", RuntimeError("db locked"))
        assert result.disposition is LaunchDisposition.RETRYABLE_FAILURE


class TestPlannerHonoursTheWindow:
    def test_open_window_launches_nothing_and_says_why(self) -> None:
        config = make_config(max_concurrent_sessions=3)
        planner = Planner(config=config, scheduler=Scheduler(config))
        window = HostRateLimitWindow()
        window.observe(_limit(T0 + timedelta(minutes=5)), T0)
        from issue_orchestrator.domain.models import PendingTechLeadReview
        from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

        anchor = PendingTechLeadReview(
            7292, "Health review", flavor=TechLeadSessionFlavor.HEALTH_REVIEW
        )
        snapshot = make_snapshot(
            issues=[make_issue(42)],
            pending_tech_lead=[anchor],
            host_rate_limit_hold=window.open_at(T0),
        )

        plan = planner.plan(snapshot)

        assert plan.actions_of_type(ActionType.LAUNCH_SESSION) == []
        reasons = {(item.item_type, item.number): item.reason for item in plan.skipped}
        assert reasons[("tech_lead", 7292)].startswith(RATE_LIMIT_DEFER_REASON)

    def test_closed_window_launches_as_before(self) -> None:
        config = make_config(max_concurrent_sessions=3)
        planner = Planner(config=config, scheduler=Scheduler(config))

        plan = planner.plan(make_snapshot(issues=[make_issue(42)]))

        assert [a.number for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)] == [42]

