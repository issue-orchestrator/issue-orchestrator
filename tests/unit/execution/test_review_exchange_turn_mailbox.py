"""Unit tests for the review-exchange TurnMailbox rendezvous.

The mailbox is the orchestrator-authoritative trust boundary that replaces
the freehand response-file channel, so every state transition and the
concurrency contract are pinned here.
"""

from __future__ import annotations

import threading

import pytest

from issue_orchestrator.execution.review_exchange_turn_mailbox import (
    DeliveryStatus,
    TurnMailbox,
)

KEY = "/wt/.issue-orchestrator/review-response.json"
OTHER_KEY = "/wt-review/.issue-orchestrator/review-response.json"


class TestHappyPath:
    def test_open_deliver_take_round_trips_payload(self) -> None:
        mailbox = TurnMailbox()
        mailbox.open(KEY, turn_id="reviewer-r1-a1")

        result = mailbox.deliver(KEY, {"response_type": "ok", "response_text": "good"})

        assert result.status is DeliveryStatus.ACCEPTED
        assert result.accepted is True
        assert result.turn_id == "reviewer-r1-a1"
        assert mailbox.try_take(KEY) == {
            "response_type": "ok",
            "response_text": "good",
        }

    def test_deliver_copies_payload_so_caller_mutation_does_not_leak(self) -> None:
        mailbox = TurnMailbox()
        mailbox.open(KEY, turn_id="t")
        payload = {"response_type": "ok"}
        mailbox.deliver(KEY, payload)
        payload["response_type"] = "mutated"

        assert mailbox.try_take(KEY) == {"response_type": "ok"}


class TestRejection:
    def test_deliver_without_open_slot_is_rejected(self) -> None:
        mailbox = TurnMailbox()
        result = mailbox.deliver(KEY, {"response_type": "ok"})
        assert result.status is DeliveryStatus.NO_OPEN_SLOT
        assert result.turn_id is None

    def test_second_deliver_into_filled_slot_is_rejected(self) -> None:
        mailbox = TurnMailbox()
        mailbox.open(KEY, turn_id="t")
        assert mailbox.deliver(KEY, {"response_type": "ok"}).accepted
        second = mailbox.deliver(KEY, {"response_type": "disagree"})
        assert second.status is DeliveryStatus.ALREADY_DELIVERED
        assert second.turn_id == "t"
        # The first verdict is the one that stands.
        assert mailbox.try_take(KEY) == {"response_type": "ok"}

    def test_deliver_after_take_but_before_close_is_rejected(self) -> None:
        # A duplicate submission arriving after the worker already consumed
        # the verdict must not overwrite or re-deliver.
        mailbox = TurnMailbox()
        mailbox.open(KEY, turn_id="t")
        mailbox.deliver(KEY, {"response_type": "ok"})
        assert mailbox.try_take(KEY) == {"response_type": "ok"}
        late = mailbox.deliver(KEY, {"response_type": "disagree"})
        assert late.status is DeliveryStatus.ALREADY_DELIVERED

    def test_deliver_after_close_is_rejected(self) -> None:
        mailbox = TurnMailbox()
        mailbox.open(KEY, turn_id="t")
        mailbox.close(KEY)
        assert mailbox.deliver(KEY, {"response_type": "ok"}).status is (
            DeliveryStatus.NO_OPEN_SLOT
        )


class TestTake:
    def test_take_before_deliver_returns_none(self) -> None:
        mailbox = TurnMailbox()
        mailbox.open(KEY, turn_id="t")
        assert mailbox.try_take(KEY) is None

    def test_take_without_open_returns_none(self) -> None:
        assert TurnMailbox().try_take(KEY) is None

    def test_take_is_one_shot(self) -> None:
        mailbox = TurnMailbox()
        mailbox.open(KEY, turn_id="t")
        mailbox.deliver(KEY, {"response_type": "ok"})
        assert mailbox.try_take(KEY) == {"response_type": "ok"}
        assert mailbox.try_take(KEY) is None

    def test_take_after_close_returns_none(self) -> None:
        mailbox = TurnMailbox()
        mailbox.open(KEY, turn_id="t")
        mailbox.deliver(KEY, {"response_type": "ok"})
        mailbox.close(KEY)
        assert mailbox.try_take(KEY) is None


class TestSupersede:
    def test_open_supersedes_undelivered_prior_turn(self) -> None:
        mailbox = TurnMailbox()
        mailbox.open(KEY, turn_id="r1")
        # New turn opens before the prior one ever received a verdict.
        mailbox.open(KEY, turn_id="r2")
        result = mailbox.deliver(KEY, {"response_type": "ok"})
        assert result.turn_id == "r2"
        assert mailbox.try_take(KEY) == {"response_type": "ok"}

    def test_open_supersedes_delivered_but_untaken_prior_turn(self) -> None:
        mailbox = TurnMailbox()
        mailbox.open(KEY, turn_id="r1")
        mailbox.deliver(KEY, {"response_type": "stale"})
        # Worker never took it (e.g. the prior turn timed out); the next turn
        # starts fresh and the stale verdict is discarded.
        mailbox.open(KEY, turn_id="r2")
        assert mailbox.try_take(KEY) is None


class TestIsolation:
    def test_distinct_keys_do_not_interfere(self) -> None:
        mailbox = TurnMailbox()
        mailbox.open(KEY, turn_id="coder")
        mailbox.open(OTHER_KEY, turn_id="reviewer")
        mailbox.deliver(KEY, {"role": "coder"})
        assert mailbox.try_take(OTHER_KEY) is None
        assert mailbox.deliver(OTHER_KEY, {"role": "reviewer"}).accepted
        assert mailbox.try_take(KEY) == {"role": "coder"}
        assert mailbox.try_take(OTHER_KEY) == {"role": "reviewer"}


class TestValidation:
    def test_open_rejects_empty_key(self) -> None:
        with pytest.raises(ValueError, match="key must be non-empty"):
            TurnMailbox().open("", turn_id="t")

    def test_open_rejects_empty_turn_id(self) -> None:
        with pytest.raises(ValueError, match="turn_id must be non-empty"):
            TurnMailbox().open(KEY, turn_id="")

    def test_close_is_idempotent(self) -> None:
        mailbox = TurnMailbox()
        mailbox.close(KEY)  # never opened
        mailbox.open(KEY, turn_id="t")
        mailbox.close(KEY)
        mailbox.close(KEY)  # double close


class TestConcurrency:
    def test_concurrent_deliveries_accept_exactly_one(self) -> None:
        # Models many simultaneous exchange-respond calls racing into the
        # same open slot: the mailbox must accept exactly one and reject the
        # rest as ALREADY_DELIVERED — never two ACCEPTED.
        mailbox = TurnMailbox()
        mailbox.open(KEY, turn_id="t")
        results = []
        results_lock = threading.Lock()
        start = threading.Barrier(16)

        def attempt(i: int) -> None:
            start.wait()
            res = mailbox.deliver(KEY, {"winner": i})
            with results_lock:
                results.append(res)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        accepted = [r for r in results if r.status is DeliveryStatus.ACCEPTED]
        rejected = [r for r in results if r.status is DeliveryStatus.ALREADY_DELIVERED]
        assert len(accepted) == 1
        assert len(rejected) == 15
        taken = mailbox.try_take(KEY)
        assert taken is not None and "winner" in taken
