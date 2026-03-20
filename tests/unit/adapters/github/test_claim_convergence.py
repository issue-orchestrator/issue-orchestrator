"""Unit tests for GitHubClaimAdapter convergence protocol."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from issue_orchestrator.adapters.github.claim_adapter import GitHubClaimAdapter
from issue_orchestrator.domain.lease_config import LeaseConfig


class MockHttpClient:
    """Mock GitHub HTTP client for testing."""

    def __init__(self):
        self.comments: list[dict] = []
        self.add_comment_calls: list[tuple[int, str]] = []

    def add_comment(self, issue_number: int, body: str) -> str:
        self.add_comment_calls.append((issue_number, body))
        self.comments.append({"body": body})
        return f"https://github.com/test/repo/issues/{issue_number}#comment"

    def get_issue_comments(self, issue_number: int, **_kwargs: object) -> list[dict]:
        return self.comments


class MockLabelAdapter:
    """Mock label adapter for testing."""

    def __init__(self):
        self.labels: dict[int, set[str]] = {}

    def add_label(self, issue_number: int, label: str) -> None:
        self.labels.setdefault(issue_number, set()).add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.labels.get(issue_number, set()).discard(label)


@pytest.fixture
def mock_client():
    return MockHttpClient()


@pytest.fixture
def mock_labels():
    return MockLabelAdapter()


@pytest.fixture
def test_config():
    """Fast convergence config for testing."""
    return LeaseConfig(
        lease_seconds=30,
        renew_interval_seconds=10,
        convergence_timeout_seconds=1.0,  # Very short for tests
        convergence_poll_min_ms=10,
        convergence_poll_max_ms=20,
        convergence_required_wins=2,
    )


@pytest.fixture
def adapter(mock_client, mock_labels, test_config):
    return GitHubClaimAdapter(
        client=mock_client,
        claimant_id="test-orchestrator",
        config=test_config,
        label_adapter=mock_labels,
    )


class TestConvergence:
    """Tests for run_convergence method."""

    def test_convergence_succeeds_after_required_wins(self, adapter, mock_client):
        """Convergence returns True after seeing ourselves as winner N times."""
        # Post our claim first
        result = adapter.attempt_claim(issue_number=42)
        assert result.success is True

        # Run convergence - should succeed because we're the only claimant
        converged = adapter.run_convergence(issue_number=42, lease_id=result.lease_id)

        assert converged is True

    @patch("time.monotonic")
    @patch("time.sleep")
    def test_convergence_fails_on_timeout(
        self, mock_sleep, mock_monotonic, mock_client, mock_labels, test_config
    ):
        """Convergence returns False if timeout is reached without enough wins."""
        # Set up mock to simulate time passing beyond timeout
        call_count = [0]

        def advancing_monotonic():
            call_count[0] += 1
            if call_count[0] <= 2:
                return 0.0  # First calls: within timeout
            return 10.0  # Later calls: past timeout

        mock_monotonic.side_effect = advancing_monotonic
        mock_sleep.return_value = None

        adapter = GitHubClaimAdapter(
            client=mock_client,
            claimant_id="test-orchestrator",
            config=test_config,
            label_adapter=mock_labels,
        )

        # Post our claim
        result = adapter.attempt_claim(issue_number=42)

        # Add a competing claim with higher priority to prevent wins
        now = datetime.now()
        higher_priority = int(now.timestamp() * 1000) + 1000000
        competing_claim = f"""```io-claim
lease_id: competing-lease-{higher_priority}
claimant: other-orchestrator
started_at: {now.strftime('%Y-%m-%dT%H:%M:%S')}
expires_at: {(now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')}
priority: {higher_priority}
```"""
        mock_client.comments.append({"body": competing_claim})

        # Run convergence - should fail because we're not the winner
        converged = adapter.run_convergence(issue_number=42, lease_id=result.lease_id)

        assert converged is False

    def test_convergence_resets_on_contested(self, mock_client, mock_labels):
        """Win counter resets when we lose winner status mid-convergence."""
        # Use config that requires 3 consecutive wins so we can see the reset
        config = LeaseConfig(
            lease_seconds=30,
            renew_interval_seconds=10,
            convergence_timeout_seconds=3.0,
            convergence_poll_min_ms=10,
            convergence_poll_max_ms=20,
            convergence_required_wins=3,  # Require 3 wins to see reset
        )

        adapter = GitHubClaimAdapter(
            client=mock_client,
            claimant_id="test-orchestrator",
            config=config,
            label_adapter=mock_labels,
        )

        # Post our claim
        result = adapter.attempt_claim(issue_number=42)
        our_lease_id = result.lease_id

        # Track fetch calls and inject/remove competing claims
        # noqa: SLF001 - Wrapping private method to simulate concurrent claims in test
        original_fetch = adapter._fetch_all_claims  # noqa: SLF001
        fetch_count = [0]

        def controlled_fetch(issue_number, **_kwargs):
            fetch_count[0] += 1

            # On fetch #1, inject competing claim BEFORE returning
            if fetch_count[0] == 1:
                now = datetime.now()
                higher_priority = int(now.timestamp() * 1000) + 1000000
                competing_claim = f"""```io-claim
lease_id: interloper-{higher_priority}
claimant: other-orchestrator
started_at: {now.strftime('%Y-%m-%dT%H:%M:%S')}
expires_at: {(now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')}
priority: {higher_priority}
```"""
                mock_client.comments.append({"body": competing_claim})

            # On fetch #2, remove competing claims to let us win
            if fetch_count[0] == 2:
                mock_client.comments = [
                    c for c in mock_client.comments
                    if "test-orchestrator" in c["body"]
                ]

            return original_fetch(issue_number)

        adapter._fetch_all_claims = controlled_fetch  # noqa: SLF001

        converged = adapter.run_convergence(issue_number=42, lease_id=our_lease_id)

        # Should eventually converge after competing claim is removed
        assert converged is True
        # Verify multiple fetches happened (at least: 1 loss + 3 wins)
        assert fetch_count[0] >= 4

    def test_requires_consecutive_wins(self, adapter, mock_client):
        """Convergence requires N consecutive wins, not N total wins."""
        # Post our claim
        result = adapter.attempt_claim(issue_number=42)

        # With only our claim, we should converge quickly
        converged = adapter.run_convergence(issue_number=42, lease_id=result.lease_id)

        assert converged is True


class TestConvergenceWithMockedTime:
    """Tests with fully mocked time for deterministic behavior."""

    @patch("time.monotonic")
    @patch("time.sleep")
    @patch("random.randint")
    def test_polls_with_jitter(
        self, mock_randint, mock_sleep, mock_monotonic, mock_client, mock_labels
    ):
        """Convergence uses random jitter between polls."""
        config = LeaseConfig(
            lease_seconds=30,
            renew_interval_seconds=10,
            convergence_timeout_seconds=5.0,
            convergence_poll_min_ms=100,
            convergence_poll_max_ms=200,
            convergence_required_wins=2,
        )

        # Simulate time advancing slowly
        time_value = [0.0]

        def mock_time():
            return time_value[0]

        def mock_sleep_fn(seconds):
            time_value[0] += seconds

        mock_monotonic.side_effect = mock_time
        mock_sleep.side_effect = mock_sleep_fn
        mock_randint.return_value = 150  # Fixed jitter for testing

        adapter = GitHubClaimAdapter(
            client=mock_client,
            claimant_id="test-orchestrator",
            config=config,
            label_adapter=mock_labels,
        )

        # Post our claim
        result = adapter.attempt_claim(issue_number=42)

        # Run convergence
        converged = adapter.run_convergence(issue_number=42, lease_id=result.lease_id)

        assert converged is True
        # Verify randint was called with correct jitter bounds
        mock_randint.assert_called_with(100, 200)

    @patch("time.monotonic")
    @patch("time.sleep")
    def test_stops_at_timeout(
        self, mock_sleep, mock_monotonic, mock_client, mock_labels
    ):
        """Convergence stops when timeout is reached."""
        config = LeaseConfig(
            lease_seconds=30,
            renew_interval_seconds=10,
            convergence_timeout_seconds=0.5,
            convergence_poll_min_ms=100,
            convergence_poll_max_ms=200,
            convergence_required_wins=2,
        )

        # Simulate time jumping past deadline immediately
        mock_monotonic.side_effect = [0.0, 1.0]  # Start, then past deadline
        mock_sleep.return_value = None

        adapter = GitHubClaimAdapter(
            client=mock_client,
            claimant_id="test-orchestrator",
            config=config,
            label_adapter=mock_labels,
        )

        # Post our claim
        result = adapter.attempt_claim(issue_number=42)

        # Add competing claim to prevent immediate win
        now = datetime.now()
        higher_priority = int(now.timestamp() * 1000) + 1000000
        mock_client.comments.append({"body": f"""```io-claim
lease_id: other-{higher_priority}
claimant: other
started_at: {now.strftime('%Y-%m-%dT%H:%M:%S')}
expires_at: {(now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')}
priority: {higher_priority}
```"""})

        # Run convergence - should timeout
        converged = adapter.run_convergence(issue_number=42, lease_id=result.lease_id)

        assert converged is False


class TestTieBreakingIntegration:
    """Integration tests for tie-breaking in convergence context."""

    @pytest.fixture(autouse=True)
    def _no_convergence_sleep(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _: None)

    def test_higher_priority_claim_wins_convergence(self, mock_client, mock_labels):
        """When competing, higher priority claim wins during convergence."""
        config = LeaseConfig.for_testing()

        # Create two adapters simulating two orchestrators
        adapter_a = GitHubClaimAdapter(
            client=mock_client,
            claimant_id="orchestrator-a",
            config=config,
            label_adapter=mock_labels,
        )

        # Adapter A claims first
        result_a = adapter_a.attempt_claim(issue_number=42)

        # Manually add adapter B's claim with higher priority
        now = datetime.now()
        b_priority = int(now.timestamp() * 1000) + 1000000
        mock_client.comments.append({"body": f"""```io-claim
lease_id: lease-b-{b_priority}
claimant: orchestrator-b
started_at: {now.strftime('%Y-%m-%dT%H:%M:%S')}
expires_at: {(now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')}
priority: {b_priority}
```"""})

        # A's convergence should fail
        converged_a = adapter_a.run_convergence(
            issue_number=42, lease_id=result_a.lease_id
        )

        assert converged_a is False

    def test_lexicographic_tiebreak_during_convergence(self, mock_client, mock_labels):
        """When priorities equal, larger lease_id wins."""
        config = LeaseConfig.for_testing()

        adapter = GitHubClaimAdapter(
            client=mock_client,
            claimant_id="orchestrator-a",
            config=config,
            label_adapter=mock_labels,
        )

        # Post claim with specific lease_id
        now = datetime.now()
        shared_priority = int(now.timestamp() * 1000)

        # Add claim with "aaa" prefix (lower lexicographically)
        mock_client.comments.append({"body": f"""```io-claim
lease_id: aaa-lease
claimant: orchestrator-a
started_at: {now.strftime('%Y-%m-%dT%H:%M:%S')}
expires_at: {(now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')}
priority: {shared_priority}
```"""})

        # Add claim with "zzz" prefix (higher lexicographically)
        mock_client.comments.append({"body": f"""```io-claim
lease_id: zzz-lease
claimant: orchestrator-b
started_at: {now.strftime('%Y-%m-%dT%H:%M:%S')}
expires_at: {(now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')}
priority: {shared_priority}
```"""})

        # Convergence for "aaa" should fail
        converged_aaa = adapter.run_convergence(issue_number=42, lease_id="aaa-lease")
        assert converged_aaa is False

        # Convergence for "zzz" should succeed
        converged_zzz = adapter.run_convergence(issue_number=42, lease_id="zzz-lease")
        assert converged_zzz is True


class TestNWayContention:
    """Tests for convergence with 3+ orchestrators contending."""

    @pytest.fixture(autouse=True)
    def _no_convergence_sleep(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _: None)

    def _inject_claim(
        self, client, claimant: str, priority: int, lease_id: str | None = None,
    ) -> str:
        """Add a claim comment to the shared mock client."""
        now = datetime.now()
        lid = lease_id or f"lease-{claimant}-{priority}"
        client.comments.append({"body": f"""```io-claim
lease_id: {lid}
claimant: {claimant}
started_at: {now.strftime('%Y-%m-%dT%H:%M:%S')}
expires_at: {(now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')}
priority: {priority}
```"""})
        return lid

    def test_three_claimants_highest_priority_wins(self, mock_client, mock_labels):
        """With 3 claimants, only the highest priority converges."""
        config = LeaseConfig.for_testing()

        # Inject 3 competing claims
        self._inject_claim(mock_client, "orchestrator-a", 1000)
        lease_b = self._inject_claim(mock_client, "orchestrator-b", 3000)
        self._inject_claim(mock_client, "orchestrator-c", 2000)

        adapter = GitHubClaimAdapter(
            client=mock_client,
            claimant_id="orchestrator-b",
            config=config,
            label_adapter=mock_labels,
        )

        # B (highest priority) should converge
        assert adapter.run_convergence(42, lease_b) is True

        # A and C should fail convergence
        adapter_a = GitHubClaimAdapter(
            client=mock_client, claimant_id="orchestrator-a",
            config=config, label_adapter=mock_labels,
        )
        assert adapter_a.run_convergence(42, "lease-orchestrator-a-1000") is False

    def test_five_claimants_deterministic_winner(self, mock_client, mock_labels):
        """With 5 claimants, the same winner is deterministic regardless of order."""
        config = LeaseConfig.for_testing()

        # Add claims in arbitrary order
        self._inject_claim(mock_client, "e", 100)
        self._inject_claim(mock_client, "c", 500)
        self._inject_claim(mock_client, "a", 300)
        self._inject_claim(mock_client, "d", 200)
        self._inject_claim(mock_client, "b", 400)

        adapter = GitHubClaimAdapter(
            client=mock_client, claimant_id="c",
            config=config, label_adapter=mock_labels,
        )

        # c has priority 500 (highest) — should converge
        assert adapter.run_convergence(42, "lease-c-500") is True

        # All others should fail
        for name, pri in [("e", 100), ("a", 300), ("d", 200), ("b", 400)]:
            a = GitHubClaimAdapter(
                client=mock_client, claimant_id=name,
                config=config, label_adapter=mock_labels,
            )
            assert a.run_convergence(42, f"lease-{name}-{pri}") is False

    def test_late_arrival_disrupts_convergence(self, mock_client, mock_labels):
        """A higher-priority claim arriving mid-convergence disrupts the leader."""
        config = LeaseConfig(
            lease_seconds=30,
            convergence_timeout_seconds=3.0,
            convergence_poll_min_ms=10,
            convergence_poll_max_ms=20,
            convergence_required_wins=3,  # Need 3 wins to see disruption
        )

        # Start with only A's claim
        lease_a = self._inject_claim(mock_client, "orchestrator-a", 1000)

        adapter_a = GitHubClaimAdapter(
            client=mock_client, claimant_id="orchestrator-a",
            config=config, label_adapter=mock_labels,
        )

        # Intercept fetches to inject late arrival after first poll
        original_comments = list(mock_client.comments)
        fetch_count = [0]
        original_get = mock_client.get_issue_comments

        def inject_late_arrival(issue_number, **kwargs):
            fetch_count[0] += 1
            if fetch_count[0] == 2:
                # Late arrival with higher priority
                self._inject_claim(mock_client, "orchestrator-z", 9000)
            return original_get(issue_number, **kwargs)

        mock_client.get_issue_comments = inject_late_arrival

        # A should fail convergence (z has higher priority after poll 2)
        converged = adapter_a.run_convergence(42, lease_a)
        assert converged is False
