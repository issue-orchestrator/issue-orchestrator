"""A person's hand re-applied needs-human outlives every cause it ended (#8774).

A person takes the shared block off and puts it back between two of the
owner's observations. Both observations read the label present, so only
GitHub's events show the change: the label's standing ``labeled`` event is a
new one. That clear ended every cause of the old generation, so releasing one
of them must leave the person's new block on.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from issue_orchestrator.control.needs_human_block import (
    BlockOutcome,
    HumanBlockRequest,
    NeedsHumanBlock,
    NeedsHumanCause,
    ValidatedWorkBlockSource,
)
from issue_orchestrator.control.human_gates import HumanGates
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.merge_hold_migration import MergeHoldMigration, MergeHoldMoveStatus
from issue_orchestrator.control.needs_human_episodes import NeedsHumanEpisodes
from issue_orchestrator.domain.human_block import HumanHoldScope
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.execution.pending_work_claim_store import SqlitePendingWorkClaimStore
from issue_orchestrator.infra.config import Config
from tests.label_application_helpers import LabelEvents

ITEM = 8774
NEEDS_HUMAN = "needs-human"

#: Every cause the owner records as a row, each as one lifecycle asks for it.
ROW_BACKED = (
    HumanBlockRequest(ITEM, NeedsHumanCause.AGENT_COMPLETION, "agent asked"),
    HumanBlockRequest(ITEM, NeedsHumanCause.MERGE_ESCALATION, "merge exhausted"),
    HumanBlockRequest(ITEM, NeedsHumanCause.SESSION_LIFECYCLE, "session gave up"),
    HumanBlockRequest(ITEM, NeedsHumanCause.MERGE_DECISION, "merge waits on a person"),
    HumanBlockRequest(ITEM, NeedsHumanCause.ACTION_LIVENESS, "action parked"),
    HumanBlockRequest(
        ITEM, NeedsHumanCause.VALIDATED_WORK_DISPOSITION, "publication failed",
        ValidatedWorkBlockSource("record-1"),
    ),
)


def test_every_row_backed_cause_is_covered() -> None:
    assert {request.cause for request in ROW_BACKED} == {
        cause for cause in NeedsHumanCause if cause.is_row_backed
    }


def _owner(github: LabelEvents, store: SqlitePendingWorkClaimStore) -> NeedsHumanBlock:
    return NeedsHumanBlock(
        needs_human_label=NEEDS_HUMAN,
        tech_lead_marker="tech-lead-needs-human",
        labels=github,
        read_labels=github.read_labels,
        quarantined_issue_numbers=frozenset,
        causes=store,
        label_application=github.label_application,
    )


@pytest.fixture
def github() -> LabelEvents:
    return LabelEvents()


@pytest.fixture
def store(tmp_path: Path) -> SqlitePendingWorkClaimStore:
    return SqlitePendingWorkClaimStore.for_repo(tmp_path)


@pytest.mark.parametrize("request_", ROW_BACKED, ids=lambda request: request.cause_key)
def test_a_release_leaves_a_block_a_person_put_back_by_hand(
    github: LabelEvents, store: SqlitePendingWorkClaimStore, request_: HumanBlockRequest
) -> None:
    block = _owner(github, store)
    assert block.acquire(request_) is BlockOutcome.HELD

    github.reapply_by_hand(ITEM, NEEDS_HUMAN)  # unseen by the owner: present both times

    assert block.release(request_) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert NEEDS_HUMAN in github.live[ITEM], "the person's new block was lifted"
    assert store.needs_human_causes(ITEM) == frozenset(), "the person's clear ended the cause"


@pytest.mark.parametrize("request_", ROW_BACKED, ids=lambda request: request.cause_key)
def test_a_release_from_the_standing_generation_takes_the_block_off(
    github: LabelEvents, store: SqlitePendingWorkClaimStore, request_: HumanBlockRequest
) -> None:
    block = _owner(github, store)
    assert block.acquire(request_) is BlockOutcome.HELD

    assert block.release(request_) is BlockOutcome.CLEARED
    assert NEEDS_HUMAN not in github.live[ITEM]
    assert store.needs_human_causes(ITEM) == frozenset()


def test_a_cause_that_joined_a_hand_placed_block_leaves_its_reapplication_alone(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    """The joining cause binds the generation then standing, so a later hand
    re-application is a different generation, not the one it joined."""
    github.add_label(ITEM, NEEDS_HUMAN)  # a person's block, no cause
    block = _owner(github, store)
    agent = ROW_BACKED[0]
    assert block.acquire(agent) is BlockOutcome.HELD

    github.reapply_by_hand(ITEM, NEEDS_HUMAN)

    assert block.release(agent) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert NEEDS_HUMAN in github.live[ITEM]


def test_a_cause_joining_after_a_reapplication_retires_the_old_generation(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    """The old cause cannot ride along into the new generation and later lift
    it; the cause that joined the new one is the one recorded on it."""
    block = _owner(github, store)
    old, new = ROW_BACKED[2], ROW_BACKED[0]
    assert block.acquire(old) is BlockOutcome.HELD
    github.reapply_by_hand(ITEM, NEEDS_HUMAN)

    assert block.acquire(new) is BlockOutcome.HELD

    assert store.needs_human_causes(ITEM) == frozenset({new.cause_key})
    assert block.release(old) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert NEEDS_HUMAN in github.live[ITEM]
    assert store.needs_human_causes(ITEM) == frozenset({new.cause_key})


def test_a_binding_that_finds_a_reapplication_retires_causes_and_removal_intent(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    """The health review's binding retires them too, in the same transaction
    that opens the new generation."""
    block = _owner(github, store)
    assert block.acquire(ROW_BACKED[2]) is BlockOutcome.HELD
    assert store.begin_needs_human_removal(ITEM) is True  # an unacknowledged removal
    before = store.needs_human_episodes([ITEM])[ITEM]
    github.reapply_by_hand(ITEM, NEEDS_HUMAN)
    standing = github.label_application(ITEM, NEEDS_HUMAN)
    assert standing is not None

    episode = store.bind_needs_human_episode(
        ITEM, event_id=standing.event_id, applied_at=standing.created_at,
    )

    assert episode != before
    assert store.needs_human_causes(ITEM) == frozenset()
    assert store.begin_needs_human_removal(ITEM) is True, "the old removal intent was retired"


def test_a_binding_to_the_same_application_keeps_the_generation_and_its_causes(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    block = _owner(github, store)
    assert block.acquire(ROW_BACKED[2]) is BlockOutcome.HELD
    before = store.needs_human_episodes([ITEM])[ITEM]
    standing = github.label_application(ITEM, NEEDS_HUMAN)
    assert standing is not None

    episode = store.bind_needs_human_episode(
        ITEM, event_id=standing.event_id, applied_at=standing.created_at,
    )

    assert episode == before
    assert store.needs_human_causes(ITEM) == frozenset({ROW_BACKED[2].cause_key})


def test_a_replayed_removal_after_a_reapplication_leaves_the_new_block(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    """The owner's removal landed on GitHub, it died before finishing, and a
    person put the label back. The intent no longer makes the replay ambiguous
    forever: the binding shows a new generation and the replay leaves it."""
    block = _owner(github, store)
    session = ROW_BACKED[2]
    assert block.acquire(session) is BlockOutcome.HELD
    assert store.begin_needs_human_removal(ITEM) is True
    github.remove_label(ITEM, NEEDS_HUMAN)  # the removal that landed
    github.add_label(ITEM, NEEDS_HUMAN)  # the person's new block

    assert block.release(session) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert NEEDS_HUMAN in github.live[ITEM]


def test_an_unreadable_event_history_keeps_the_block_and_its_cause(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    block = _owner(github, store)
    session = ROW_BACKED[2]
    assert block.acquire(session) is BlockOutcome.HELD
    github.events_unreadable = True

    assert block.release(session) is BlockOutcome.FAILED
    assert NEEDS_HUMAN in github.live[ITEM]
    assert store.needs_human_causes(ITEM) == frozenset({session.cause_key})

    github.events_unreadable = False
    assert block.release(session) is BlockOutcome.CLEARED


def test_a_join_whose_generation_is_unknown_records_nothing(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    github.add_label(ITEM, NEEDS_HUMAN)
    github.events_unreadable = True
    block = _owner(github, store)

    assert block.acquire(ROW_BACKED[0]) is BlockOutcome.FAILED
    assert store.needs_human_causes(ITEM) == frozenset()
    assert NEEDS_HUMAN in github.live[ITEM]


def test_an_owner_label_whose_events_lag_is_still_held(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    """Binding the owner's own write is best effort: the block is on and its
    cause recorded whether or not GitHub's events can be read right after."""
    github.events_unreadable = True
    block = _owner(github, store)

    assert block.acquire(ROW_BACKED[2]) is BlockOutcome.HELD
    assert NEEDS_HUMAN in github.live[ITEM]
    assert store.needs_human_causes(ITEM) == frozenset({ROW_BACKED[2].cause_key})


@pytest.mark.parametrize("request_", ROW_BACKED, ids=lambda request: request.cause_key)
def test_a_release_of_a_cause_never_recorded_leaves_a_hand_placed_block(
    github: LabelEvents, store: SqlitePendingWorkClaimStore, request_: HumanBlockRequest
) -> None:
    """Generalises the liveness rule (#7350) to every row-backed cause."""
    github.add_label(ITEM, NEEDS_HUMAN)
    block = _owner(github, store)

    assert block.release(request_) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert NEEDS_HUMAN in github.live[ITEM]


def test_a_release_of_an_absent_block_reports_it_cleared(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    github.live[ITEM] = set()
    block = _owner(github, store)

    assert block.release(ROW_BACKED[1]) is BlockOutcome.CLEARED


def test_a_resolution_does_not_discharge_a_cause_from_a_prior_generation(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    block = _owner(github, store)
    session = ROW_BACKED[2]
    assert block.acquire(session) is BlockOutcome.HELD
    github.reapply_by_hand(ITEM, NEEDS_HUMAN)

    outcome = block.resolve(ITEM, frozenset({session.cause}), "the tech lead decided")

    assert outcome.mutation_attempted is False
    assert NEEDS_HUMAN in github.live[ITEM]


def test_a_resolution_of_the_standing_generation_lifts_it(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    block = _owner(github, store)
    session = ROW_BACKED[2]
    assert block.acquire(session) is BlockOutcome.HELD

    outcome = block.resolve(ITEM, frozenset({session.cause}), "the tech lead decided")

    assert outcome.outcome is BlockOutcome.CLEARED
    assert NEEDS_HUMAN not in github.live[ITEM]


def _episodes(github: LabelEvents, store: SqlitePendingWorkClaimStore) -> NeedsHumanEpisodes:
    config = Config()
    config.repo = "porchpin/porchpin"
    return NeedsHumanEpisodes(
        store=store, label_applications=github.label_applications, labels=LabelManager(config),
    )


def _snapshot(*labels: str) -> dict[int, Issue]:
    return {ITEM: Issue(number=ITEM, title="item", labels=list(labels), repo="porchpin/porchpin", state="open")}


def test_a_health_review_that_finds_a_reapplication_ends_the_old_causes(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    block = _owner(github, store)
    session = ROW_BACKED[2]
    assert block.acquire(session) is BlockOutcome.HELD
    github.reapply_by_hand(ITEM, NEEDS_HUMAN)

    episodes, _ = _episodes(github, store).verified(_snapshot(NEEDS_HUMAN), {})

    assert ITEM in episodes
    assert store.needs_human_causes(ITEM) == frozenset()
    assert block.release(session) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert NEEDS_HUMAN in github.live[ITEM]


def test_a_stale_snapshot_dated_by_the_marker_does_not_end_the_standing_causes(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    """A snapshot from before the block went back on shows only the tech-lead
    marker. Binding the marker's event would read as a hand re-application of
    needs-human and retire causes GitHub shows still standing: the episode is
    dated by the needs-human application GitHub shows."""
    marker = "tech-lead-needs-human"
    github.add_label(ITEM, marker)
    block = _owner(github, store)
    session = ROW_BACKED[2]
    assert block.acquire(session) is BlockOutcome.HELD

    before = store.needs_human_episodes([ITEM])[ITEM]

    episodes, _ = _episodes(github, store).verified(_snapshot(marker), {})

    assert episodes[ITEM] == before
    assert store.needs_human_causes(ITEM) == frozenset({session.cause_key})


def test_an_owner_generation_left_unbound_does_not_adopt_a_later_reapplication(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    """r1 F1: the owner's own write could not be bound (events unreadable right
    after it). A person's re-application, well after the owner opened the
    generation, is not that write: the stale cause leaves it on."""
    block = _owner(github, store)
    session = ROW_BACKED[2]
    github.events_unreadable = True
    assert block.acquire(session) is BlockOutcome.HELD
    github.events_unreadable = False

    github.later = timedelta(minutes=30)
    github.reapply_by_hand(ITEM, NEEDS_HUMAN)

    assert block.release(session) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert NEEDS_HUMAN in github.live[ITEM]
    assert store.needs_human_causes(ITEM) == frozenset()


def test_an_owner_generation_left_unbound_does_not_adopt_a_quick_reapplication(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    """r2 F1: a re-application moments after the owner's unverified write
    looks the same as that write, so no later binding adopts it."""
    block = _owner(github, store)
    session = ROW_BACKED[2]
    github.events_unreadable = True
    assert block.acquire(session) is BlockOutcome.HELD
    github.events_unreadable = False
    github.reapply_by_hand(ITEM, NEEDS_HUMAN)  # within seconds

    assert block.release(session) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert NEEDS_HUMAN in github.live[ITEM]


def test_an_owner_write_never_bound_leaves_its_block_to_a_person(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    """Failing closed: with its own write unverified, the owner cannot tell
    its block from a person's, so it leaves the label on."""
    block = _owner(github, store)
    session = ROW_BACKED[2]
    github.events_unreadable = True
    assert block.acquire(session) is BlockOutcome.HELD
    github.events_unreadable = False

    assert block.release(session) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert NEEDS_HUMAN in github.live[ITEM]


def test_an_agent_question_a_person_ended_is_not_moved_to_a_pr_merge_hold(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    """r1 F3: the issue's agent_completion row belongs to a generation a person
    ended by putting the label back by hand. The move acts on that row's
    ownership, so it reads the standing generation and refuses."""
    pr = 8775
    block = _owner(github, store)
    assert block.acquire(ROW_BACKED[0]) is BlockOutcome.HELD
    github.reapply_by_hand(ITEM, NEEDS_HUMAN)
    github.live[pr] = {"code-reviewed"}

    def item(number: int) -> Issue:
        return Issue(number=number, title="item", labels=github.read_labels(number), repo="o/r", state="open")

    migration = MergeHoldMigration(
        block=block, labels=LabelManager(Config()), read_issue=item, read_pr=item,
        pr_issue_number=lambda _pr: ITEM,
    )

    moved = migration.move(ITEM, pr, apply=True)

    assert moved.status is MergeHoldMoveStatus.REFUSED, moved.detail
    assert "no recorded cause" in moved.detail
    assert NEEDS_HUMAN in github.live[ITEM] and NEEDS_HUMAN not in github.live[pr]


def test_an_unreadable_generation_refuses_the_move(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    pr = 8775
    block = _owner(github, store)
    assert block.acquire(ROW_BACKED[0]) is BlockOutcome.HELD
    github.live[pr] = {"code-reviewed"}
    github.events_unreadable = True

    def item(number: int) -> Issue:
        return Issue(number=number, title="item", labels=github.read_labels(number), repo="o/r", state="open")

    migration = MergeHoldMigration(
        block=block, labels=LabelManager(Config()), read_issue=item, read_pr=item,
        pr_issue_number=lambda _pr: ITEM,
    )

    moved = migration.move(ITEM, pr, apply=True)

    assert moved.status is MergeHoldMoveStatus.REFUSED
    assert "cannot be verified" in moved.detail
    assert NEEDS_HUMAN not in github.live[pr]


@pytest.mark.parametrize(
    "cause", [NeedsHumanCause.TECH_LEAD_ESCALATION, NeedsHumanCause.CLAIM_QUARANTINE],
)
def test_a_self_recording_release_leaves_a_block_a_person_put_back_by_hand(
    github: LabelEvents, store: SqlitePendingWorkClaimStore, cause: NeedsHumanCause
) -> None:
    """r2 F2: a hand-over or a quarantine releasing after the person's
    re-application. Its marker or ledger row said it wanted a block; the
    person's new one is not its own to take off."""
    quarantined: set[int] = {ITEM} if cause is NeedsHumanCause.CLAIM_QUARANTINE else set()
    block = NeedsHumanBlock(
        needs_human_label=NEEDS_HUMAN, tech_lead_marker="tech-lead-needs-human", labels=github,
        read_labels=github.read_labels, quarantined_issue_numbers=lambda: frozenset(quarantined),
        causes=store, label_application=github.label_application,
    )
    request = HumanBlockRequest(ITEM, cause, "held")
    assert block.acquire(request) is BlockOutcome.HELD
    github.reapply_by_hand(ITEM, NEEDS_HUMAN)
    quarantined.clear()  # the quarantine resolves; a hand-over has no marker left

    assert block.release(request) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert NEEDS_HUMAN in github.live[ITEM]


@pytest.mark.parametrize(
    "cause", [NeedsHumanCause.TECH_LEAD_ESCALATION, NeedsHumanCause.CLAIM_QUARANTINE],
)
def test_a_self_recording_release_of_its_own_generation_takes_the_block_off(
    github: LabelEvents, store: SqlitePendingWorkClaimStore, cause: NeedsHumanCause
) -> None:
    block = _owner(github, store)
    request = HumanBlockRequest(ITEM, cause, "held")
    assert block.acquire(request) is BlockOutcome.HELD

    assert block.release(request) is BlockOutcome.CLEARED
    assert NEEDS_HUMAN not in github.live[ITEM]


def _merge_held_pr(github: LabelEvents, store: SqlitePendingWorkClaimStore) -> tuple[NeedsHumanBlock, HumanGates]:
    block = _owner(github, store)
    assert block.acquire(HumanBlockRequest(ITEM, NeedsHumanCause.MERGE_DECISION, "merge waits")) is BlockOutcome.HELD
    return block, HumanGates.over(block, LabelManager(Config()))


def test_a_merge_hold_a_person_put_back_by_hand_holds_the_prs_work(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    """r2 F3: the PR's only record is a merge decision, from the generation
    the person ended. Review and rework must not proceed under their block."""
    _block, gates = _merge_held_pr(github, store)
    assert gates.needs_human_scope(ITEM, github.read_labels(ITEM)) is HumanHoldScope.MERGE
    _block.merge_scope_checked.clear()  # the next check is due
    github.reapply_by_hand(ITEM, NEEDS_HUMAN)

    assert gates.needs_human_scope(ITEM, github.read_labels(ITEM)) is HumanHoldScope.WORK
    assert gates.holds_work(ITEM, github.read_labels(ITEM))


def test_an_unverifiable_merge_hold_holds_the_work(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    _block, gates = _merge_held_pr(github, store)
    github.events_unreadable = True

    assert gates.needs_human_scope(ITEM, github.read_labels(ITEM)) is HumanHoldScope.WORK


def test_a_standing_merge_hold_is_rechecked_at_most_once_per_period(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    _block, gates = _merge_held_pr(github, store)
    reads: list[int] = []
    standing = github.label_application

    def counted(number: int, label: str):
        reads.append(number)
        return standing(number, label)

    github.label_application = counted  # type: ignore[method-assign]
    block = _owner(github, store)
    gates = HumanGates.over(block, LabelManager(Config()))

    for _ in range(3):
        assert gates.needs_human_scope(ITEM, github.read_labels(ITEM)) is HumanHoldScope.MERGE
    assert reads == [ITEM]


def test_a_cause_row_from_before_generations_does_not_lift_a_block(
    github: LabelEvents, store: SqlitePendingWorkClaimStore
) -> None:
    """r3 F2: a row written before generations were recorded has none, so a
    binding cannot place it on the application standing now; it is retired
    and its release leaves the label to a person."""
    github.add_label(ITEM, NEEDS_HUMAN)
    session = ROW_BACKED[2]
    store.record_needs_human_cause(ITEM, session.cause_key, reason="before generations")
    assert store.needs_human_episodes([ITEM]) == {}
    github.reapply_by_hand(ITEM, NEEDS_HUMAN)

    assert _owner(github, store).release(session) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert NEEDS_HUMAN in github.live[ITEM]
    assert store.needs_human_causes(ITEM) == frozenset()
