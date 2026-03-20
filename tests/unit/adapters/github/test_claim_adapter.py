"""Unit tests for adapters/github/claim_adapter.py."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.adapters.github.claim_adapter import GitHubClaimAdapter
from issue_orchestrator.domain.claim import Claim, ClaimFetchError, ClaimState
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
        self.add_label_calls: list[tuple[int, str]] = []
        self.remove_label_calls: list[tuple[int, str]] = []

    def add_label(self, issue_number: int, label: str) -> None:
        self.add_label_calls.append((issue_number, label))
        self.labels.setdefault(issue_number, set()).add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.remove_label_calls.append((issue_number, label))
        self.labels.get(issue_number, set()).discard(label)


@pytest.fixture
def mock_client():
    return MockHttpClient()


@pytest.fixture
def mock_labels():
    return MockLabelAdapter()


@pytest.fixture
def adapter(mock_client, mock_labels):
    return GitHubClaimAdapter(
        client=mock_client,
        claimant_id="test-orchestrator",
        config=LeaseConfig.for_testing(),
        label_adapter=mock_labels,
    )


class TestAttemptClaim:
    """Tests for attempt_claim method."""

    def test_creates_claim_comment(self, adapter, mock_client):
        """attempt_claim posts a claim comment."""
        result = adapter.attempt_claim(issue_number=42)

        assert result.success is True
        assert result.lease_id is not None
        assert len(mock_client.add_comment_calls) == 1
        issue_num, body = mock_client.add_comment_calls[0]
        assert issue_num == 42
        assert "io-claim" in body
        assert "test-orchestrator" in body

    def test_adds_claim_label(self, adapter, mock_labels):
        """attempt_claim adds the io:claimed label."""
        adapter.attempt_claim(issue_number=42)

        assert len(mock_labels.add_label_calls) == 1
        issue_num, label = mock_labels.add_label_calls[0]
        assert issue_num == 42
        assert label == "io:claimed"

    def test_generates_unique_lease_id(self, adapter):
        """Each claim gets a unique lease_id."""
        result1 = adapter.attempt_claim(issue_number=42)
        result2 = adapter.attempt_claim(issue_number=43)

        assert result1.lease_id != result2.lease_id

    def test_result_state_is_claiming(self, adapter):
        """Result state is CLAIMING (not yet converged)."""
        result = adapter.attempt_claim(issue_number=42)

        assert result.state == ClaimState.CLAIMING

    def test_returns_failure_on_exception(self, mock_labels):
        """Returns failed result if posting comment fails."""
        mock_client = MockHttpClient()
        mock_client.add_comment = MagicMock(side_effect=Exception("Network error"))

        adapter = GitHubClaimAdapter(
            client=mock_client,
            claimant_id="test-orchestrator",
            config=LeaseConfig.for_testing(),
            label_adapter=mock_labels,
        )

        result = adapter.attempt_claim(issue_number=42)

        assert result.success is False
        assert result.state == ClaimState.UNCLAIMED
        assert "Network error" in result.error


class TestReleaseClaim:
    """Tests for release_claim method."""

    def test_removes_claim_label(self, adapter, mock_labels):
        """release_claim removes the io:claimed label."""
        # First claim
        adapter.attempt_claim(issue_number=42)

        # Then release
        adapter.release_claim(issue_number=42, lease_id="test-lease")

        assert len(mock_labels.remove_label_calls) == 1
        issue_num, label = mock_labels.remove_label_calls[0]
        assert issue_num == 42
        assert label == "io:claimed"


class TestCheckWinner:
    """Tests for check_winner method."""

    def test_returns_true_when_winner(self, adapter, mock_client):
        """Returns True when we are the current winner."""
        # Post our claim
        result = adapter.attempt_claim(issue_number=42)

        # Check if we're winner
        is_winner = adapter.check_winner(
            issue_number=42, lease_id=result.lease_id
        )

        assert is_winner is True

    def test_returns_false_when_not_winner(self, adapter, mock_client):
        """Returns False when someone else's claim wins."""
        # Our claim first (will have current epoch ms as priority)
        result = adapter.attempt_claim(issue_number=42)

        # Manually add another orchestrator's claim with higher priority
        # Use a priority higher than what we just got
        now = datetime.now()
        higher_priority = int(now.timestamp() * 1000) + 1000000  # 1000 seconds in future
        other_claim = f"""```io-claim
lease_id: other-lease-{higher_priority}
claimant: other-orchestrator
started_at: {now.strftime('%Y-%m-%dT%H:%M:%S')}
expires_at: {(now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')}
priority: {higher_priority}
```"""
        mock_client.comments.append({"body": other_claim})

        # We should not be winner (other has higher priority)
        is_winner = adapter.check_winner(
            issue_number=42, lease_id=result.lease_id
        )

        assert is_winner is False


class TestRenewClaim:
    """Tests for renew_claim method."""

    def test_renews_when_we_are_winner(self, adapter, mock_client):
        """renew_claim succeeds when we own the claim."""
        # First claim
        result = adapter.attempt_claim(issue_number=42)

        # Renew
        success = adapter.renew_claim(issue_number=42, lease_id=result.lease_id)

        assert success is True
        # Should have posted a second comment (renewal)
        assert len(mock_client.add_comment_calls) == 2

    def test_fails_when_not_winner(self, adapter, mock_client):
        """renew_claim fails when we don't own the claim."""
        # Add someone else's winning claim with high priority
        now = datetime.now()
        high_priority = int(now.timestamp() * 1000) + 1000000  # 1000 seconds in future
        other_claim = f"""```io-claim
lease_id: other-lease-{high_priority}
claimant: other-orchestrator
started_at: {now.strftime('%Y-%m-%dT%H:%M:%S')}
expires_at: {(now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')}
priority: {high_priority}
```"""
        mock_client.comments.append({"body": other_claim})

        # Try to renew a lease we don't own
        success = adapter.renew_claim(issue_number=42, lease_id="my-fake-lease")

        assert success is False


def _make_claim(
    claimant: str,
    priority: int,
    lease_id: str | None = None,
    expired: bool = False,
    issue_number: int = 42,
) -> Claim:
    """Helper to create a Claim with minimal boilerplate."""
    now = datetime.now()
    expires = now - timedelta(hours=1) if expired else now + timedelta(hours=1)
    return Claim(
        lease_id=lease_id or f"lease-{claimant}-{priority}",
        claimant=claimant,
        issue_number=issue_number,
        started_at=now,
        expires_at=expires,
        priority=priority,
    )


class TestDetermineWinner:
    """Tests for _determine_winner edge cases.

    These test the winner-determination logic through the public
    get_current_claim method (which calls _determine_winner internally).
    """

    @pytest.fixture
    def adapter_with_claims(self, mock_labels):
        """Create an adapter with a mock client that returns pre-set claims."""
        client = MockHttpClient()
        adapter = GitHubClaimAdapter(
            client=client,
            claimant_id="test",
            config=LeaseConfig.for_testing(),
            label_adapter=mock_labels,
        )
        return adapter, client

    def _inject_claims(self, client: MockHttpClient, claims: list[Claim]) -> None:
        """Inject claim comments into mock client."""
        from issue_orchestrator.adapters.github.claim_parser import format_claim_comment
        client.comments = [{"body": format_claim_comment(c)} for c in claims]

    def test_no_claims_returns_none(self, adapter_with_claims):
        """No claims → no winner."""
        adapter, _client = adapter_with_claims
        assert adapter.get_current_claim(42) is None

    def test_all_expired_returns_none(self, adapter_with_claims):
        """All claims expired → no winner."""
        adapter, client = adapter_with_claims
        self._inject_claims(client, [
            _make_claim("a", 1000, expired=True),
            _make_claim("b", 2000, expired=True),
        ])
        assert adapter.get_current_claim(42) is None

    def test_single_valid_among_expired(self, adapter_with_claims):
        """Single valid claim among expired ones wins."""
        adapter, client = adapter_with_claims
        valid = _make_claim("b", 1000)
        self._inject_claims(client, [
            _make_claim("a", 3000, expired=True),  # Higher priority but expired
            valid,
            _make_claim("c", 2000, expired=True),
        ])
        winner = adapter.get_current_claim(42)
        assert winner is not None
        assert winner.claimant == "b"

    def test_highest_priority_wins(self, adapter_with_claims):
        """Highest priority among valid claims wins."""
        adapter, client = adapter_with_claims
        self._inject_claims(client, [
            _make_claim("a", 1000),
            _make_claim("b", 3000),
            _make_claim("c", 2000),
        ])
        winner = adapter.get_current_claim(42)
        assert winner is not None
        assert winner.claimant == "b"

    def test_five_claimants_highest_wins(self, adapter_with_claims):
        """Among 5 claimants, highest priority wins."""
        adapter, client = adapter_with_claims
        self._inject_claims(client, [
            _make_claim("a", 100),
            _make_claim("b", 500),
            _make_claim("c", 300),
            _make_claim("d", 200),
            _make_claim("e", 400),
        ])
        winner = adapter.get_current_claim(42)
        assert winner is not None
        assert winner.claimant == "b"

    def test_equal_priority_lexicographic_tiebreak(self, adapter_with_claims):
        """Equal priority → lexicographically larger lease_id wins."""
        adapter, client = adapter_with_claims
        self._inject_claims(client, [
            _make_claim("a", 1000, lease_id="aaa-lease"),
            _make_claim("b", 1000, lease_id="zzz-lease"),
            _make_claim("c", 1000, lease_id="mmm-lease"),
        ])
        winner = adapter.get_current_claim(42)
        assert winner is not None
        assert winner.lease_id == "zzz-lease"

    def test_mixed_expired_and_valid_with_priorities(self, adapter_with_claims):
        """Mix of expired/valid claims with varying priorities."""
        adapter, client = adapter_with_claims
        self._inject_claims(client, [
            _make_claim("expired-high", 9000, expired=True),
            _make_claim("valid-low", 100),
            _make_claim("valid-high", 500),
            _make_claim("expired-med", 300, expired=True),
            _make_claim("valid-med", 300),
        ])
        winner = adapter.get_current_claim(42)
        assert winner is not None
        assert winner.claimant == "valid-high"


class TestAPIFailureResilience:
    """Tests for behavior when GitHub API calls fail.

    Verifies that transient API failures don't cause false claim loss.
    """

    @pytest.fixture
    def failing_client(self):
        """Mock client that raises on get_issue_comments."""
        client = MockHttpClient()
        client.get_issue_comments = MagicMock(
            side_effect=Exception("GitHub API 502")
        )
        return client

    @pytest.fixture
    def adapter_with_failing_api(self, failing_client, mock_labels):
        return GitHubClaimAdapter(
            client=failing_client,
            claimant_id="test-orchestrator",
            config=LeaseConfig.for_testing(),
            label_adapter=mock_labels,
        )

    def test_check_winner_returns_true_on_api_error(self, adapter_with_failing_api):
        """check_winner returns True (benefit of the doubt) on API error."""
        result = adapter_with_failing_api.check_winner(42, "my-lease")
        assert result is True

    def test_get_current_claim_raises_on_api_error(self, adapter_with_failing_api):
        """get_current_claim raises ClaimFetchError on API error."""
        with pytest.raises(ClaimFetchError, match="GitHub API 502"):
            adapter_with_failing_api.get_current_claim(42)

    def test_renew_claim_raises_on_api_error(self, adapter_with_failing_api):
        """renew_claim raises ClaimFetchError when it can't verify ownership."""
        with pytest.raises(ClaimFetchError):
            adapter_with_failing_api.renew_claim(42, "my-lease")

    @patch("time.sleep")
    def test_convergence_resets_wins_on_api_error(self, mock_sleep, mock_labels):
        """Convergence resets consecutive wins on API error (doesn't count as win)."""
        client = MockHttpClient()
        call_count = [0]
        original_get = client.get_issue_comments

        def intermittent_fail(issue_number, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:  # Fail on second poll
                raise Exception("GitHub API 503")
            return original_get(issue_number, **kwargs)

        client.get_issue_comments = intermittent_fail

        config = LeaseConfig(
            lease_seconds=30,
            convergence_timeout_seconds=5.0,
            convergence_poll_min_ms=10,
            convergence_poll_max_ms=20,
            convergence_required_wins=2,
        )
        adapter = GitHubClaimAdapter(
            client=client,
            claimant_id="test",
            config=config,
            label_adapter=mock_labels,
        )

        # Post our claim
        result = adapter.attempt_claim(42)

        # Convergence should still succeed (API error resets wins but
        # doesn't end convergence — keeps polling)
        converged = adapter.run_convergence(42, result.lease_id)
        assert converged is True
        # Should have needed more polls due to the reset
        assert call_count[0] >= 3

    @patch("time.sleep")
    def test_convergence_fails_if_api_always_errors(self, mock_sleep, mock_labels):
        """Convergence fails if API is consistently unreachable."""
        client = MockHttpClient()
        client.get_issue_comments = MagicMock(
            side_effect=Exception("GitHub down")
        )

        config = LeaseConfig(
            lease_seconds=30,
            convergence_timeout_seconds=0.5,
            convergence_poll_min_ms=10,
            convergence_poll_max_ms=20,
            convergence_required_wins=2,
            convergence_max_polls=5,
        )
        adapter = GitHubClaimAdapter(
            client=client,
            claimant_id="test",
            config=config,
            label_adapter=mock_labels,
        )

        # Post claim (uses add_comment which still works on this client)
        client.add_comment = MockHttpClient().add_comment.__get__(client)

        converged = adapter.run_convergence(42, "test-lease")
        assert converged is False
