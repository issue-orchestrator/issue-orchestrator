"""Unit tests for LeaseRenewer."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.lease_renewer import LeaseRenewer
from issue_orchestrator.domain.claim import ClaimFetchError
from issue_orchestrator.domain.lease_config import LeaseConfig
from issue_orchestrator.domain.models import Issue, Session, SessionKey, TaskKind
from tests.unit.session_run_helpers import make_session_run_assets


class MockClaimManager:
    """Mock ClaimManager for testing."""

    def __init__(self, renew_success: bool = True, is_winner: bool = True):
        self._renew_success = renew_success
        self._is_winner = is_winner
        self.renew_claim_calls: list[tuple[int, str]] = []
        self.check_winner_calls: list[tuple[int, str]] = []

    def renew_claim(self, issue_number: int, lease_id: str) -> bool:
        self.renew_claim_calls.append((issue_number, lease_id))
        return self._renew_success

    def check_winner(self, issue_number: int, lease_id: str) -> bool:
        self.check_winner_calls.append((issue_number, lease_id))
        return self._is_winner


class MockEventSink:
    """Mock event sink for testing."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def publish(self, event):
        self.events.append((event.event_type, event.data))


def create_test_session(
    issue_number: int = 42,
    lease_id: str | None = "test-lease",
    expires_in_seconds: float = 300,
    lease_acquired_seconds_ago: float = 300,  # 5 minutes ago by default
    last_verified_seconds_ago: float | None = None,  # None = never verified
) -> Session:
    """Create a test session with configurable lease expiry and verification times."""
    issue_key = MagicMock()
    issue_key.stable_id.return_value = f"issue-{issue_number}"
    session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)

    now = datetime.now()
    lease_acquired_at = now - timedelta(seconds=lease_acquired_seconds_ago) if lease_id else None
    lease_expires_at = now + timedelta(seconds=expires_in_seconds) if lease_id else None
    last_claim_verified_at = (
        now - timedelta(seconds=last_verified_seconds_ago)
        if last_verified_seconds_ago is not None else None
    )

    return Session(
        key=session_key,
        issue=Issue(number=issue_number, title=f"Issue #{issue_number}", labels=["test"]),
        agent_config=MagicMock(command="test"),
        terminal_id=f"issue-{issue_number}",
        worktree_path=Path("/tmp/worktree"),
        branch_name="test-branch",
        run_assets=make_session_run_assets(
            Path("/tmp/worktree"),
            session_name=f"issue-{issue_number}",
        ),
        completion_path="completion.json",
        agent_label="test-agent",
        lease_id=lease_id,
        lease_acquired_at=lease_acquired_at,
        lease_expires_at=lease_expires_at,
        last_claim_verified_at=last_claim_verified_at,
    )


class TestCheckRenewals:
    """Tests for LeaseRenewer.check_renewals."""

    @pytest.fixture
    def config(self):
        return LeaseConfig(
            lease_seconds=900,  # 15 minutes
            renew_interval_seconds=300,  # 5 minutes
        )

    def test_renews_when_within_threshold(self, config):
        """Renews lease when within renewal threshold."""
        claim_manager = MockClaimManager(renew_success=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        # Create session expiring soon (within 300s threshold)
        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,  # 200s remaining < 600s threshold
        )

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 0
        assert len(claim_manager.renew_claim_calls) == 1
        assert claim_manager.renew_claim_calls[0] == (42, "test-lease")

    def test_skips_renewal_when_not_needed(self, config):
        """Does not renew when plenty of time remaining."""
        claim_manager = MockClaimManager(renew_success=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        # Create session with lots of time remaining
        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=800,  # 800s remaining > 600s threshold
        )

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 0
        assert len(claim_manager.renew_claim_calls) == 0

    def test_returns_lost_sessions_on_renewal_failure(self, config):
        """Returns sessions that fail renewal."""
        claim_manager = MockClaimManager(renew_success=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,  # Within renewal threshold
        )

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 1
        assert lost_sessions[0] == session

    def test_updates_expires_at_on_success(self, config):
        """Updates session.lease_expires_at on successful renewal."""
        claim_manager = MockClaimManager(renew_success=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,
        )
        old_expiry = session.lease_expires_at

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 0
        # Expiry should have been extended
        assert session.lease_expires_at > old_expiry

    def test_skips_sessions_without_lease(self, config):
        """Skips sessions that don't have a lease_id."""
        claim_manager = MockClaimManager(renew_success=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(
            issue_number=42,
            lease_id=None,  # No lease
        )

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 0
        assert len(claim_manager.renew_claim_calls) == 0

    def test_handles_multiple_sessions(self, config):
        """Handles multiple sessions correctly."""
        claim_manager = MockClaimManager(renew_success=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        sessions = [
            create_test_session(42, "lease-a", 200),  # Needs renewal
            create_test_session(43, "lease-b", 800),  # No renewal needed
            create_test_session(44, "lease-c", 100),  # Needs renewal
        ]

        lost_sessions = renewer.check_renewals(sessions)

        assert len(lost_sessions) == 0
        # Only sessions 42 and 44 should be renewed
        assert len(claim_manager.renew_claim_calls) == 2
        issue_numbers = [call[0] for call in claim_manager.renew_claim_calls]
        assert 42 in issue_numbers
        assert 44 in issue_numbers
        assert 43 not in issue_numbers

    def test_emits_renewal_event(self, config):
        """Emits CLAIM_RENEWED event on successful renewal."""
        claim_manager = MockClaimManager(renew_success=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,
        )

        renewer.check_renewals([session])

        assert len(events.events) == 1
        event_type, data = events.events[0]
        assert "renewed" in str(event_type).lower()
        assert data["issue_number"] == 42
        assert data["lease_id"] == "test-lease"

    def test_emits_claim_lost_event_on_failure(self, config):
        """Emits CLAIM_LOST event when renewal fails."""
        claim_manager = MockClaimManager(renew_success=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,
        )

        renewer.check_renewals([session])

        assert len(events.events) == 1
        event_type, data = events.events[0]
        assert "lost" in str(event_type).lower()
        assert data["issue_number"] == 42
        assert data["reason"] == "renewal_failed"


class TestCheckSingleSession:
    """Tests for LeaseRenewer.check_single_session."""

    @pytest.fixture
    def config(self):
        return LeaseConfig.for_testing()

    def test_returns_true_when_winner(self, config):
        """Returns True when session is still the claim winner."""
        claim_manager = MockClaimManager(is_winner=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(issue_number=42, lease_id="test-lease")

        result = renewer.check_single_session(session)

        assert result is True
        assert len(claim_manager.check_winner_calls) == 1

    def test_returns_false_when_not_winner(self, config):
        """Returns False when session has lost the claim."""
        claim_manager = MockClaimManager(is_winner=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(issue_number=42, lease_id="test-lease")

        result = renewer.check_single_session(session)

        assert result is False
        assert len(claim_manager.check_winner_calls) == 1

    def test_returns_true_when_no_lease(self, config):
        """Returns True when session has no lease (no claim system)."""
        claim_manager = MockClaimManager(is_winner=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(issue_number=42, lease_id=None)

        result = renewer.check_single_session(session)

        assert result is True
        assert len(claim_manager.check_winner_calls) == 0

    def test_emits_event_when_claim_lost(self, config):
        """Emits CLAIM_LOST event when claim check fails."""
        claim_manager = MockClaimManager(is_winner=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(issue_number=42, lease_id="test-lease")

        renewer.check_single_session(session)

        assert len(events.events) == 1
        event_type, data = events.events[0]
        assert "lost" in str(event_type).lower()
        assert data["reason"] == "on_demand_check"


class TestPeriodicVerification:
    """Tests for periodic claim verification at lease/3 intervals."""

    @pytest.fixture
    def config(self):
        return LeaseConfig(
            lease_seconds=900,  # 15 minutes
            renew_interval_seconds=300,  # 5 minutes
        )

    def test_verifies_claim_at_lease_third_interval(self, config):
        """Verifies claim when lease/3 seconds (5 min) have passed since acquisition."""
        claim_manager = MockClaimManager(renew_success=True, is_winner=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        # Session acquired 6 minutes ago (> 5 min = lease/3), never verified
        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=540,  # 9 min remaining
            lease_acquired_seconds_ago=360,  # 6 min ago
            last_verified_seconds_ago=None,  # Never verified
        )

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 0
        # Should have called check_winner for periodic verification
        assert len(claim_manager.check_winner_calls) == 1
        assert claim_manager.check_winner_calls[0] == (42, "test-lease")
        # Should have updated last_claim_verified_at
        assert session.last_claim_verified_at is not None

    def test_skips_verification_when_recently_verified(self, config):
        """Does not verify if less than lease/3 seconds since last verification."""
        claim_manager = MockClaimManager(renew_success=True, is_winner=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        # Session verified 2 minutes ago (< 5 min = lease/3)
        # renewal_threshold = 900 - 300 = 600s, so 700s remaining is outside renewal window
        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=700,  # 11.6 min remaining (> 600s renewal threshold)
            lease_acquired_seconds_ago=200,  # 3.3 min ago (< 300s = lease/3)
            last_verified_seconds_ago=120,  # 2 min ago (< 300s = lease/3)
        )

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 0
        # Should NOT have called check_winner (not due for verification)
        assert len(claim_manager.check_winner_calls) == 0
        # Should NOT have called renew_claim (not in renewal window)
        assert len(claim_manager.renew_claim_calls) == 0

    def test_detects_claim_loss_during_periodic_verification(self, config):
        """Returns session as lost if claim lost during periodic verification."""
        claim_manager = MockClaimManager(renew_success=True, is_winner=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        # Session needs verification (6 min since last)
        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=540,  # Not in renewal window
            lease_acquired_seconds_ago=720,  # 12 min ago
            last_verified_seconds_ago=360,  # 6 min since last verification
        )

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 1
        assert lost_sessions[0] == session
        # Should have emitted claim lost event
        assert len(events.events) == 1
        event_type, data = events.events[0]
        assert "lost" in str(event_type).lower()
        assert data["reason"] == "periodic_verification"

    def test_skips_renewal_after_claim_loss_in_verification(self, config):
        """Does not attempt renewal if claim was lost during periodic verification."""
        claim_manager = MockClaimManager(renew_success=True, is_winner=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        # Session needs verification AND is in renewal window
        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,  # In renewal window (< 600s threshold)
            lease_acquired_seconds_ago=720,  # 12 min ago
            last_verified_seconds_ago=360,  # 6 min since last verification
        )

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 1
        # Should have checked winner (verification)
        assert len(claim_manager.check_winner_calls) == 1
        # Should NOT have attempted renewal (claim lost in verification)
        assert len(claim_manager.renew_claim_calls) == 0

    def test_renewal_updates_last_claim_verified_at(self, config):
        """Successful renewal also updates last_claim_verified_at."""
        claim_manager = MockClaimManager(renew_success=True, is_winner=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        # Session in renewal window, recently verified
        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,  # In renewal window
            lease_acquired_seconds_ago=720,  # 12 min ago
            last_verified_seconds_ago=60,  # 1 min ago (not due for verification)
        )
        old_verified = session.last_claim_verified_at

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 0
        # Renewal should have updated last_claim_verified_at
        assert session.last_claim_verified_at is not None
        assert session.last_claim_verified_at > old_verified


class TestFailOpenLiveness:
    """LeaseRenewer must fail OPEN on API errors — don't kill sessions
    when ownership can't be verified due to transient GitHub outages."""

    @pytest.fixture
    def config(self):
        return LeaseConfig(lease_seconds=900, renew_interval_seconds=300)

    def test_periodic_verification_fails_open_on_api_error(self, config):
        """API error during periodic verification keeps session alive."""

        class FailingClaimManager:
            def check_winner(self, issue_number, lease_id):
                raise ClaimFetchError("GitHub 502")

            def renew_claim(self, issue_number, lease_id):
                return True

        events = MockEventSink()
        renewer = LeaseRenewer(FailingClaimManager(), events, config)

        # Session needs verification (6 min since last > 5 min = lease/3)
        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=540,
            lease_acquired_seconds_ago=720,
            last_verified_seconds_ago=360,
        )

        lost_sessions = renewer.check_renewals([session])

        # Should NOT report as lost — fail-open for liveness
        assert len(lost_sessions) == 0

    def test_on_demand_check_fails_open_on_api_error(self, config):
        """check_single_session returns True on API error."""

        class FailingClaimManager:
            def check_winner(self, issue_number, lease_id):
                raise ClaimFetchError("GitHub 502")

        events = MockEventSink()
        renewer = LeaseRenewer(FailingClaimManager(), events, config)

        session = create_test_session(issue_number=42, lease_id="test-lease")

        result = renewer.check_single_session(session)
        assert result is True


class TestLeaseRenewalGaps:
    """Tests for edge cases in renewal timing and API failure recovery."""

    @pytest.fixture
    def config(self):
        return LeaseConfig(
            lease_seconds=900,  # 15 minutes
            renew_interval_seconds=300,  # 5 minutes
        )

    def test_session_running_for_hours_gets_renewed(self, config):
        """Session running 2+ hours still gets renewed on each tick."""
        claim_manager = MockClaimManager(renew_success=True, is_winner=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        # Session started 2 hours ago, lease about to expire
        session = create_test_session(
            issue_number=42,
            lease_id="long-running-lease",
            expires_in_seconds=100,  # Within renewal window (< 600)
            lease_acquired_seconds_ago=7200,  # 2 hours ago
            last_verified_seconds_ago=60,  # Recently verified
        )

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 0
        assert len(claim_manager.renew_claim_calls) == 1
        # Expiry should be extended
        assert session.lease_expires_at > datetime.now()

    def test_rapid_successive_ticks_dont_double_renew(self, config):
        """Two rapid ticks: first renews, second should not (expiry extended)."""
        claim_manager = MockClaimManager(renew_success=True, is_winner=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,  # Within renewal window
            lease_acquired_seconds_ago=720,
            last_verified_seconds_ago=60,
        )

        # First tick: renewal happens
        renewer.check_renewals([session])
        assert len(claim_manager.renew_claim_calls) == 1

        # Session now has a fresh expiry (900s from now) — well outside renewal window
        # Second tick immediately after: should NOT renew
        renewer.check_renewals([session])
        assert len(claim_manager.renew_claim_calls) == 1  # Still just 1

    def test_api_error_during_renewal_does_not_report_loss(self, config):
        """API error during renewal skips session instead of reporting it as lost."""
        claim_manager = MockClaimManager(renew_success=True, is_winner=True)
        claim_manager.renew_claim = MagicMock(
            side_effect=ClaimFetchError("GitHub 502")
        )
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,  # Within renewal window
            lease_acquired_seconds_ago=720,
            last_verified_seconds_ago=60,
        )

        lost_sessions = renewer.check_renewals([session])

        # Should NOT report as lost — just skip and retry next tick
        assert len(lost_sessions) == 0

    def test_renewal_recovery_after_transient_failure(self, config):
        """Renewal succeeds on next tick after a transient API failure."""
        call_count = [0]

        class RecoveringClaimManager:
            check_winner_calls: list = []

            def check_winner(self, issue_number, lease_id):
                self.check_winner_calls.append((issue_number, lease_id))
                return True

            def renew_claim(self, issue_number, lease_id):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise ClaimFetchError("transient error")
                return True

        claim_manager = RecoveringClaimManager()
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,
            lease_acquired_seconds_ago=720,
            last_verified_seconds_ago=60,
        )

        # First tick: fails with ClaimFetchError → not reported as lost
        lost = renewer.check_renewals([session])
        assert len(lost) == 0

        # Second tick: succeeds
        lost = renewer.check_renewals([session])
        assert len(lost) == 0
        assert call_count[0] == 2
