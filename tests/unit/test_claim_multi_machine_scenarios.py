"""Multi-machine claim scenario tests.

These test realistic scenarios that arise when multiple orchestrators on
different machines coordinate via the claim system. Each scenario tests
a specific failure mode or race condition that could occur in production.
"""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.adapters.github.claim_adapter import GitHubClaimAdapter
from issue_orchestrator.adapters.github.claim_parser import format_claim_comment
from issue_orchestrator.control.claim_gate import ClaimGate, ClaimLostError
from issue_orchestrator.control.lease_renewer import LeaseRenewer
from issue_orchestrator.domain.claim import Claim, ClaimFetchError
from issue_orchestrator.domain.lease_config import LeaseConfig
from issue_orchestrator.domain.models import Issue, Session, SessionKey, TaskKind
from issue_orchestrator.ports.claim_manager import NullClaimManager


# --- Helpers ---


class MockHttpClient:
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
    def __init__(self):
        self.labels: dict[int, set[str]] = {}

    def add_label(self, issue_number: int, label: str) -> None:
        self.labels.setdefault(issue_number, set()).add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.labels.get(issue_number, set()).discard(label)


class MockEventSink:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def make_claim(
    claimant: str,
    priority: int,
    lease_id: str | None = None,
    expires_at: datetime | None = None,
    issue_number: int = 42,
) -> Claim:
    now = datetime.now()
    return Claim(
        lease_id=lease_id or f"lease-{claimant}-{priority}",
        claimant=claimant,
        issue_number=issue_number,
        started_at=now,
        expires_at=expires_at or now + timedelta(hours=1),
        priority=priority,
    )


def make_session(
    issue_number: int = 42,
    lease_id: str = "test-lease",
    expires_in: float = 200,
) -> Session:
    issue_key = MagicMock()
    issue_key.stable_id.return_value = f"issue-{issue_number}"
    key = SessionKey(issue=issue_key, task=TaskKind.CODE)
    now = datetime.now()
    return Session(
        key=key,
        issue=Issue(number=issue_number, title=f"Issue #{issue_number}", labels=["test"]),
        agent_config=MagicMock(command="test"),
        terminal_id=f"issue-{issue_number}",
        worktree_path=Path("/tmp/worktree"),
        branch_name="test-branch",
        completion_path="completion.json",
        agent_label="test-agent",
        lease_id=lease_id,
        lease_acquired_at=now - timedelta(hours=1),
        lease_expires_at=now + timedelta(seconds=expires_in),
        last_claim_verified_at=now - timedelta(seconds=60),
    )


# --- Scenario 1: Clock Skew Between Machines ---


class TestClockSkew:
    """When machines have different clocks, claim expiry is ambiguous.

    Machine A's clock: 12:00 — sees claim as valid (expires 12:15)
    Machine B's clock: 12:16 — sees same claim as expired
    Both could think they're the winner.
    """

    def test_claim_valid_on_one_clock_expired_on_another(self):
        """A claim near its boundary can be valid or expired depending on clock."""
        base = datetime(2024, 6, 15, 12, 0, 0)
        claim = Claim(
            lease_id="machine-a-lease",
            claimant="machine-a",
            issue_number=42,
            started_at=base,
            expires_at=base + timedelta(minutes=15),
            priority=1000,
        )

        # Machine A's clock: 12:14 — claim is valid
        assert not claim.is_expired(now=base + timedelta(minutes=14))

        # Machine B's clock: 12:16 — same claim appears expired
        assert claim.is_expired(now=base + timedelta(minutes=16))

    def test_skewed_clocks_can_produce_different_winners(self):
        """Two machines with skewed clocks can see different winners."""
        base = datetime(2024, 6, 15, 12, 0, 0)

        # Claim A: expires at 12:15, priority 1000
        claim_a = Claim(
            lease_id="lease-a",
            claimant="machine-a",
            issue_number=42,
            started_at=base,
            expires_at=base + timedelta(minutes=15),
            priority=1000,
        )
        # Claim B: expires at 12:30, priority 500
        claim_b = Claim(
            lease_id="lease-b",
            claimant="machine-b",
            issue_number=42,
            started_at=base + timedelta(minutes=5),
            expires_at=base + timedelta(minutes=30),
            priority=500,
        )

        client = MockHttpClient()
        labels = MockLabelAdapter()
        adapter = GitHubClaimAdapter(
            client=client, claimant_id="observer",
            config=LeaseConfig.for_testing(), label_adapter=labels,
        )

        client.comments = [
            {"body": format_claim_comment(claim_a)},
            {"body": format_claim_comment(claim_b)},
        ]

        # At 12:14 (both valid): A wins (higher priority)
        # Use the adapter's internal method via get_current_claim
        # which calls _determine_winner with now=datetime.now()
        # We test the deterministic path by calling _determine_winner directly
        # noqa: SLF001 - testing _determine_winner with explicit now
        winner_early = adapter._determine_winner(  # noqa: SLF001
            [claim_a, claim_b], now=base + timedelta(minutes=14),
        )
        assert winner_early is not None
        assert winner_early.claimant == "machine-a"

        # At 12:16 (A expired, B still valid): B wins by default
        winner_late = adapter._determine_winner(  # noqa: SLF001
            [claim_a, claim_b], now=base + timedelta(minutes=16),
        )
        assert winner_late is not None
        assert winner_late.claimant == "machine-b"

    def test_generous_lease_mitigates_clock_skew(self):
        """With 15-min lease and 5-min renewal, 1-min skew is harmless."""
        base = datetime(2024, 6, 15, 12, 0, 0)
        claim = Claim(
            lease_id="lease-a",
            claimant="machine-a",
            issue_number=42,
            started_at=base,
            expires_at=base + timedelta(minutes=15),
            priority=1000,
        )

        # Even 1 minute of clock skew: claim has 14 minutes remaining
        skewed_now = base + timedelta(minutes=1)
        assert not claim.is_expired(now=skewed_now)

        # Even 5 minutes of clock skew: still 10 minutes remaining
        skewed_now = base + timedelta(minutes=5)
        assert not claim.is_expired(now=skewed_now)


# --- Scenario 2: Renewal Race with New Claimant ---


class TestRenewalRace:
    """Machine A renews while Machine B posts a new higher-priority claim."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _: None)

    def test_new_higher_priority_claim_wins_over_renewal(self):
        """After A renews and B claims, B wins if B has higher priority."""
        client = MockHttpClient()
        labels = MockLabelAdapter()
        config = LeaseConfig.for_testing()

        # Machine A claims and converges
        adapter_a = GitHubClaimAdapter(
            client=client, claimant_id="machine-a",
            config=config, label_adapter=labels,
        )
        result_a = adapter_a.attempt_claim(42)
        assert adapter_a.run_convergence(42, result_a.lease_id) is True

        # Machine A renews (posts another comment with same lease_id)
        assert adapter_a.renew_claim(42, result_a.lease_id) is True

        # Machine B claims with higher priority
        now = datetime.now()
        b_priority = int(now.timestamp() * 1000) + 2_000_000
        b_claim = f"""```io-claim
lease_id: lease-b-{b_priority}
claimant: machine-b
started_at: {now.strftime('%Y-%m-%dT%H:%M:%S')}
expires_at: {(now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')}
priority: {b_priority}
```"""
        client.comments.append({"body": b_claim})

        # Now A should no longer be winner
        assert adapter_a.check_winner(42, result_a.lease_id) is False

    def test_renewal_preserves_ownership_without_contention(self):
        """Renewal keeps the same winner when no contention."""
        client = MockHttpClient()
        labels = MockLabelAdapter()
        config = LeaseConfig.for_testing()

        adapter = GitHubClaimAdapter(
            client=client, claimant_id="solo",
            config=config, label_adapter=labels,
        )
        result = adapter.attempt_claim(42)
        assert adapter.run_convergence(42, result.lease_id) is True

        # Renew
        assert adapter.renew_claim(42, result.lease_id) is True

        # Still the winner (renewal comment + original, both have same lease_id)
        assert adapter.check_winner(42, result.lease_id) is True


# --- Scenario 3: Claim Expiry During Convergence ---


class TestClaimExpiryDuringConvergence:
    """Our own claim could expire while we're in the convergence loop."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _: None)

    def test_expired_claim_cannot_converge(self):
        """If our claim expires before convergence completes, we don't win."""
        client = MockHttpClient()
        labels = MockLabelAdapter()

        # Very short lease: claim expires almost immediately
        config = LeaseConfig(
            lease_seconds=0,  # Expires immediately!
            convergence_timeout_seconds=1.0,
            convergence_poll_min_ms=10,
            convergence_poll_max_ms=20,
            convergence_required_wins=2,
        )

        adapter = GitHubClaimAdapter(
            client=client, claimant_id="short-lived",
            config=config, label_adapter=labels,
        )

        result = adapter.attempt_claim(42)
        # Our claim expires instantly, so we'll never see ourselves as winner
        converged = adapter.run_convergence(42, result.lease_id)
        assert converged is False


# --- Scenario 4: Full Claim Lifecycle ---


class TestFullClaimLifecycle:
    """Integration test: claim -> converge -> verify -> renew -> release."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _: None)

    def test_complete_lifecycle(self):
        """Walk through the entire claim lifecycle."""
        client = MockHttpClient()
        labels = MockLabelAdapter()
        config = LeaseConfig.for_testing()

        adapter = GitHubClaimAdapter(
            client=client, claimant_id="orchestrator-1",
            config=config, label_adapter=labels,
        )

        # 1. Claim
        result = adapter.attempt_claim(42)
        assert result.success is True
        assert result.lease_id is not None
        assert "io:claimed" in labels.labels.get(42, set())

        # 2. Converge
        converged = adapter.run_convergence(42, result.lease_id)
        assert converged is True

        # 3. Verify ownership
        assert adapter.check_winner(42, result.lease_id) is True

        # 4. Renew
        assert adapter.renew_claim(42, result.lease_id) is True
        # Renewal adds another comment
        assert len(client.add_comment_calls) >= 2

        # Still the winner after renewal
        assert adapter.check_winner(42, result.lease_id) is True

        # 5. Release
        adapter.release_claim(42, result.lease_id)
        assert "io:claimed" not in labels.labels.get(42, set())

    def test_lifecycle_with_contention_during_convergence(self):
        """Lifecycle where contention causes convergence to fail."""
        client = MockHttpClient()
        labels = MockLabelAdapter()
        config = LeaseConfig.for_testing()

        adapter_a = GitHubClaimAdapter(
            client=client, claimant_id="a",
            config=config, label_adapter=labels,
        )

        # A claims
        result_a = adapter_a.attempt_claim(42)

        # B claims with higher priority before A converges
        now = datetime.now()
        b_pri = int(now.timestamp() * 1000) + 5_000_000
        client.comments.append({"body": f"""```io-claim
lease_id: lease-b-{b_pri}
claimant: b
started_at: {now.strftime('%Y-%m-%dT%H:%M:%S')}
expires_at: {(now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')}
priority: {b_pri}
```"""})

        # A's convergence should fail
        converged = adapter_a.run_convergence(42, result_a.lease_id)
        assert converged is False

        # A should release its claim
        adapter_a.release_claim(42, result_a.lease_id)


# --- Scenario 5: NullClaimManager Contract ---


class TestNullClaimManagerContract:
    """NullClaimManager must never raise ClaimFetchError and must satisfy
    the protocol for all callers (ClaimGate, LeaseRenewer, stale detection)."""

    def test_check_winner_never_raises(self):
        """check_winner returns True, never raises."""
        mgr = NullClaimManager()
        assert mgr.check_winner(42, "any-lease") is True

    def test_get_current_claim_never_raises(self):
        """get_current_claim returns None, never raises."""
        mgr = NullClaimManager()
        assert mgr.get_current_claim(42) is None

    def test_renew_claim_never_raises(self):
        """renew_claim returns True, never raises."""
        mgr = NullClaimManager()
        assert mgr.renew_claim(42, "any-lease") is True

    def test_works_with_claim_gate(self):
        """ClaimGate allows writes when backed by NullClaimManager."""
        mgr = NullClaimManager()
        events = MockEventSink()
        gate = ClaimGate(mgr, events)

        # Should allow write (NullClaimManager.check_winner returns True)
        assert gate.verify_before_write(42, "any-lease", "push") is True

    def test_works_with_lease_renewer(self):
        """LeaseRenewer doesn't report losses with NullClaimManager."""
        mgr = NullClaimManager()
        events = MockEventSink()
        config = LeaseConfig(lease_seconds=900, renew_interval_seconds=300)
        renewer = LeaseRenewer(mgr, events, config)

        session = make_session(expires_in=200)
        lost = renewer.check_renewals([session])

        assert len(lost) == 0

    def test_attempt_claim_returns_immediately_claimed(self):
        """NullClaimManager claims instantly — no convergence needed."""
        mgr = NullClaimManager()
        result = mgr.attempt_claim(42)

        assert result.success is True
        assert result.lease_id is not None
        from issue_orchestrator.domain.claim import ClaimState
        assert result.state == ClaimState.CLAIMED  # Not CLAIMING

    def test_convergence_always_succeeds(self):
        """No convergence needed — always returns True."""
        mgr = NullClaimManager()
        assert mgr.run_convergence(42, "any-lease") is True


# --- Scenario 6: Parser Robustness Under Adversarial Input ---


class TestParserRobustness:
    """Claim parser must handle garbage, adversarial, and edge-case input."""

    def test_negative_priority(self):
        """Negative priority is parsed correctly (lower than any real claim)."""
        from issue_orchestrator.adapters.github.claim_parser import parse_claim_comment

        body = """```io-claim
lease_id: negative-test
claimant: evil
started_at: 2024-01-01T00:00:00
expires_at: 2025-01-01T00:00:00
priority: -999
```"""
        claim = parse_claim_comment(body, issue_number=42)
        assert claim is not None
        assert claim.priority == -999

    def test_huge_priority(self):
        """Very large priority values are handled."""
        from issue_orchestrator.adapters.github.claim_parser import parse_claim_comment

        body = """```io-claim
lease_id: huge-test
claimant: ambitious
started_at: 2024-01-01T00:00:00
expires_at: 2025-01-01T00:00:00
priority: 99999999999999999
```"""
        claim = parse_claim_comment(body, issue_number=42)
        assert claim is not None
        assert claim.priority == 99999999999999999

    def test_extra_fields_ignored(self):
        """Extra fields in YAML don't break parsing."""
        from issue_orchestrator.adapters.github.claim_parser import parse_claim_comment

        body = """```io-claim
lease_id: extra-fields
claimant: test
started_at: 2024-01-01T00:00:00
expires_at: 2025-01-01T00:00:00
priority: 1000
secret_sauce: 42
attack_vector: "'; DROP TABLE claims;--"
```"""
        claim = parse_claim_comment(body, issue_number=42)
        assert claim is not None
        assert claim.lease_id == "extra-fields"

    def test_unicode_claimant_id(self):
        """Unicode in claimant names doesn't break parsing."""
        from issue_orchestrator.adapters.github.claim_parser import parse_claim_comment

        body = """```io-claim
lease_id: unicode-test
claimant: "orchestrator-\u00e9\u00e8\u00ea"
started_at: 2024-01-01T00:00:00
expires_at: 2025-01-01T00:00:00
priority: 1000
```"""
        claim = parse_claim_comment(body, issue_number=42)
        assert claim is not None

    def test_claim_embedded_in_long_comment(self):
        """Claim block buried in a long comment with other content."""
        from issue_orchestrator.adapters.github.claim_parser import parse_claim_comment

        noise = "Lorem ipsum dolor sit amet. " * 100
        body = f"""{noise}

Here's the claim:

```io-claim
lease_id: buried
claimant: test
started_at: 2024-01-01T00:00:00
expires_at: 2025-01-01T00:00:00
priority: 1000
```

{noise}
"""
        claim = parse_claim_comment(body, issue_number=42)
        assert claim is not None
        assert claim.lease_id == "buried"


# --- Scenario 7: Same Claimant Renewal Creates Two Comments ---


class TestRenewalCommentAccumulation:
    """When an orchestrator renews, both old and new comments exist.
    The protocol must correctly handle multiple claims from the same claimant."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _: None)

    def test_renewal_with_expired_original(self):
        """Original claim expired, renewal valid — renewal wins."""
        now = datetime.now()

        original = Claim(
            lease_id="lease-a",
            claimant="orch-a",
            issue_number=42,
            started_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),  # Expired
            priority=1000,
        )
        renewal = Claim(
            lease_id="lease-a",  # Same lease_id
            claimant="orch-a",
            issue_number=42,
            started_at=now - timedelta(hours=2),
            expires_at=now + timedelta(hours=1),  # Valid
            priority=1000,  # Same priority
        )

        client = MockHttpClient()
        labels = MockLabelAdapter()
        adapter = GitHubClaimAdapter(
            client=client, claimant_id="orch-a",
            config=LeaseConfig.for_testing(), label_adapter=labels,
        )
        client.comments = [
            {"body": format_claim_comment(original)},
            {"body": format_claim_comment(renewal)},
        ]

        # Should still be winner (renewal is valid)
        assert adapter.check_winner(42, "lease-a") is True

    def test_multiple_renewals_accumulate(self):
        """Multiple renewals: all older ones expired, latest is valid."""
        now = datetime.now()

        comments = []
        for i in range(5):
            expired = i < 4  # First 4 expired, last one valid
            c = Claim(
                lease_id="lease-a",
                claimant="orch-a",
                issue_number=42,
                started_at=now - timedelta(hours=5),
                expires_at=(now - timedelta(hours=1)) if expired else (now + timedelta(hours=1)),
                priority=1000,
            )
            comments.append({"body": format_claim_comment(c)})

        client = MockHttpClient()
        client.comments = comments
        labels = MockLabelAdapter()
        adapter = GitHubClaimAdapter(
            client=client, claimant_id="orch-a",
            config=LeaseConfig.for_testing(), label_adapter=labels,
        )

        # Latest renewal is valid → still winner
        assert adapter.check_winner(42, "lease-a") is True
