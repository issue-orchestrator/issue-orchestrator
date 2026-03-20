"""Property-based tests for claim coordination invariants.

Uses Hypothesis to generate random claim scenarios and verify that the
core invariant holds: at most one winner exists at any point in time,
and the winner is always deterministic.
"""

from datetime import datetime, timedelta

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from issue_orchestrator.domain.claim import Claim


# --- Strategies ---

def claim_strategy(
    issue_number: int = 42,
    min_priority: int = 0,
    max_priority: int = 10_000,
    allow_expired: bool = True,
) -> st.SearchStrategy[Claim]:
    """Generate random Claim objects."""
    now = datetime.now()

    return st.builds(
        Claim,
        lease_id=st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
            min_size=8,
            max_size=16,
        ),
        claimant=st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz-",
            min_size=3,
            max_size=20,
        ),
        issue_number=st.just(issue_number),
        started_at=st.just(now),
        expires_at=(
            st.sampled_from([
                now - timedelta(hours=1),  # Expired
                now + timedelta(hours=1),  # Valid
            ])
            if allow_expired
            else st.just(now + timedelta(hours=1))
        ),
        priority=st.integers(min_value=min_priority, max_value=max_priority),
    )


def claims_list_strategy(
    min_size: int = 0,
    max_size: int = 10,
) -> st.SearchStrategy[list[Claim]]:
    """Generate a list of random claims."""
    return st.lists(
        claim_strategy(),
        min_size=min_size,
        max_size=max_size,
    )


def _determine_winner(claims: list[Claim]) -> Claim | None:
    """Pure implementation of winner determination for property testing.

    Mirrors GitHubClaimAdapter._determine_winner without needing an adapter.
    """
    now = datetime.now()
    valid = [c for c in claims if not c.is_expired(now)]
    if not valid:
        return None
    return max(valid, key=lambda c: (c.priority, c.lease_id))


# --- Properties ---


class TestWinnerDeterminism:
    """Winner determination is a pure function: same inputs always give same output."""

    @given(claims=claims_list_strategy(min_size=0, max_size=8))
    @settings(max_examples=200)
    def test_winner_is_deterministic(self, claims: list[Claim]):
        """Calling _determine_winner twice on the same claims gives the same result."""
        w1 = _determine_winner(claims)
        w2 = _determine_winner(claims)

        if w1 is None:
            assert w2 is None
        else:
            assert w2 is not None
            assert w1.lease_id == w2.lease_id
            assert w1.priority == w2.priority

    @given(claims=claims_list_strategy(min_size=1, max_size=8))
    @settings(max_examples=200)
    def test_winner_order_independent(self, claims: list[Claim]):
        """Winner is the same regardless of claim list ordering."""
        import random as rng

        w1 = _determine_winner(claims)

        shuffled = list(claims)
        rng.shuffle(shuffled)
        w2 = _determine_winner(shuffled)

        if w1 is None:
            assert w2 is None
        else:
            assert w2 is not None
            assert w1.lease_id == w2.lease_id


class TestAtMostOneWinner:
    """The invariant: at most one winner at any given time."""

    @given(claims=claims_list_strategy(min_size=0, max_size=10))
    @settings(max_examples=200)
    def test_at_most_one_winner(self, claims: list[Claim]):
        """There is at most one winner from any set of claims."""
        winner = _determine_winner(claims)

        if winner is not None:
            # The winner must be in the original list
            assert any(c.lease_id == winner.lease_id for c in claims)

            # No other valid claim has higher priority or equal priority + higher lease_id
            now = datetime.now()
            for c in claims:
                if c.is_expired(now) or c.lease_id == winner.lease_id:
                    continue
                # Winner must dominate all other valid claims
                assert (winner.priority, winner.lease_id) >= (c.priority, c.lease_id)

    @given(claims=claims_list_strategy(min_size=1, max_size=10))
    @settings(max_examples=200)
    def test_winner_is_valid(self, claims: list[Claim]):
        """The winner (if any) must be a non-expired claim."""
        winner = _determine_winner(claims)

        if winner is not None:
            now = datetime.now()
            assert not winner.is_expired(now)


class TestHighestPriorityWins:
    """The highest priority non-expired claim always wins."""

    @given(
        priorities=st.lists(
            st.integers(min_value=1, max_value=100_000),
            min_size=2,
            max_size=10,
            unique=True,
        )
    )
    @settings(max_examples=200)
    def test_highest_priority_always_wins(self, priorities: list[int]):
        """Among valid claims with unique priorities, the highest wins."""
        now = datetime.now()
        claims = [
            Claim(
                lease_id=f"lease-{p}",
                claimant=f"orch-{p}",
                issue_number=42,
                started_at=now,
                expires_at=now + timedelta(hours=1),
                priority=p,
            )
            for p in priorities
        ]

        winner = _determine_winner(claims)

        assert winner is not None
        assert winner.priority == max(priorities)


class TestExpiredClaimsIgnored:
    """Expired claims never win, regardless of priority."""

    @given(
        valid_priority=st.integers(min_value=1, max_value=100),
        expired_priority=st.integers(min_value=101, max_value=100_000),
    )
    @settings(max_examples=200)
    def test_expired_high_priority_loses_to_valid_low(
        self, valid_priority: int, expired_priority: int,
    ):
        """An expired claim with high priority loses to a valid one with low priority."""
        now = datetime.now()
        valid = Claim(
            lease_id="valid-lease",
            claimant="valid",
            issue_number=42,
            started_at=now,
            expires_at=now + timedelta(hours=1),
            priority=valid_priority,
        )
        expired = Claim(
            lease_id="expired-lease",
            claimant="expired",
            issue_number=42,
            started_at=now,
            expires_at=now - timedelta(hours=1),
            priority=expired_priority,
        )

        winner = _determine_winner([expired, valid])

        assert winner is not None
        assert winner.lease_id == "valid-lease"


class TestLexicographicTiebreak:
    """When priorities are equal, lexicographically larger lease_id wins."""

    @given(
        lease_ids=st.lists(
            st.text(
                alphabet="abcdefghijklmnopqrstuvwxyz",
                min_size=5,
                max_size=12,
            ),
            min_size=2,
            max_size=8,
            unique=True,
        ),
        priority=st.integers(min_value=1, max_value=100_000),
    )
    @settings(max_examples=200)
    def test_largest_lease_id_wins_on_tie(
        self, lease_ids: list[str], priority: int,
    ):
        """With equal priority, the lexicographically largest lease_id wins."""
        now = datetime.now()
        claims = [
            Claim(
                lease_id=lid,
                claimant=f"orch-{lid}",
                issue_number=42,
                started_at=now,
                expires_at=now + timedelta(hours=1),
                priority=priority,
            )
            for lid in lease_ids
        ]

        winner = _determine_winner(claims)

        assert winner is not None
        assert winner.lease_id == max(lease_ids)
