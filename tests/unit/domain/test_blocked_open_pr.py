"""The ledger that owns "which open PRs the scanner keeps skipping" (#7294)."""

from datetime import UTC, datetime

import pytest

from issue_orchestrator.domain.blocked_open_pr import (
    BlockedOpenPRLedger,
    BlockedOpenPRObservation,
    BlockedPRLane,
    BlockedPRSkipReason,
)

T1 = datetime(2026, 9, 23, 5, 53, tzinfo=UTC)
T2 = datetime(2026, 9, 23, 6, 3, tzinfo=UTC)
T3 = datetime(2026, 9, 23, 6, 13, tzinfo=UTC)


def _observation(
    pr_number: int,
    *,
    lane: BlockedPRLane = BlockedPRLane.REVIEW,
    issue_number: int = 320,
    labels: tuple[str, ...] = ("blocked-failed",),
) -> BlockedOpenPRObservation:
    return BlockedOpenPRObservation(
        lane=lane,
        issue_number=issue_number,
        issue_title="Halted exchange",
        pr_number=pr_number,
        pr_url=f"https://github.com/o/r/pull/{pr_number}",
        draft=False,
        skip_reason=BlockedPRSkipReason.ISSUE_BLOCKED,
        blocking_labels=labels,
    )


def test_consecutive_scans_count_skips_and_keep_the_first_timestamp() -> None:
    ledger = BlockedOpenPRLedger()

    for at in (T1, T2, T3):
        ledger.record_scan(BlockedPRLane.REVIEW, [_observation(376)], at=at)

    (entry,) = ledger.entries()
    assert entry.skip_count == 3
    assert entry.first_skipped_at == T1.isoformat()
    assert entry.last_skipped_at == T3.isoformat()


def test_a_pr_the_scan_no_longer_reports_leaves_and_restarts_its_count() -> None:
    ledger = BlockedOpenPRLedger()
    ledger.record_scan(BlockedPRLane.REVIEW, [_observation(376)], at=T1)
    ledger.record_scan(BlockedPRLane.REVIEW, [], at=T2)
    assert ledger.entries() == ()

    ledger.record_scan(BlockedPRLane.REVIEW, [_observation(376)], at=T3)
    (entry,) = ledger.entries()
    assert (entry.skip_count, entry.first_skipped_at) == (1, T3.isoformat())


def test_a_scan_of_one_lane_leaves_the_other_lane_alone() -> None:
    ledger = BlockedOpenPRLedger()
    ledger.record_scan(
        BlockedPRLane.REWORK, [_observation(378, lane=BlockedPRLane.REWORK)], at=T1
    )
    ledger.record_scan(BlockedPRLane.REVIEW, [_observation(378)], at=T2)
    ledger.record_scan(BlockedPRLane.REVIEW, [], at=T3)

    (entry,) = ledger.entries()
    assert entry.observation.lane is BlockedPRLane.REWORK
    assert entry.skip_count == 1


def test_entries_are_ordered_by_issue_then_pr() -> None:
    ledger = BlockedOpenPRLedger()
    ledger.record_scan(
        BlockedPRLane.REVIEW,
        [_observation(379, issue_number=364), _observation(376, issue_number=320)],
        at=T1,
    )

    assert [e.observation.pr_number for e in ledger.entries()] == [376, 379]


def test_an_observation_from_another_lane_is_rejected() -> None:
    with pytest.raises(ValueError, match="rework observation"):
        BlockedOpenPRLedger().record_scan(
            BlockedPRLane.REVIEW, [_observation(376, lane=BlockedPRLane.REWORK)], at=T1
        )


def test_a_scan_reporting_one_pr_twice_is_rejected() -> None:
    with pytest.raises(ValueError, match="twice"):
        BlockedOpenPRLedger().record_scan(
            BlockedPRLane.REVIEW, [_observation(376), _observation(376)], at=T1
        )


def test_an_observation_without_a_blocking_label_is_rejected() -> None:
    with pytest.raises(ValueError, match="no blocking label"):
        _observation(376, labels=())
