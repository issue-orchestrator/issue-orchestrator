"""The issue cache's ledger orders label writes against the reads they race (#8113)."""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.issue_cache_ledger import IssueCacheLedger, LabelWrite

REMOVE_BLOCK = LabelWrite(issue_number=2, label="blocked", present=False)
ADD_HUMAN = LabelWrite(issue_number=3, label="needs-human", present=True)


def test_a_write_with_no_read_in_flight_is_not_kept() -> None:
    ledger = IssueCacheLedger()

    ledger.record(REMOVE_BLOCK)

    assert ledger.kept_writes == 0


def test_a_read_in_flight_sees_the_writes_after_it_opened_in_order() -> None:
    ledger = IssueCacheLedger()
    ledger.record(LabelWrite(issue_number=9, label="before", present=True))

    opened = ledger.open_fetch()
    ledger.record(REMOVE_BLOCK)
    ledger.record(ADD_HUMAN)

    assert ledger.writes_since(opened) == (REMOVE_BLOCK, ADD_HUMAN)


def test_closing_the_read_releases_its_writes() -> None:
    ledger = IssueCacheLedger()
    opened = ledger.open_fetch()
    ledger.record(REMOVE_BLOCK)

    ledger.close_fetch(opened)

    assert ledger.kept_writes == 0


def test_a_write_is_kept_until_the_oldest_read_that_predates_it_closes() -> None:
    ledger = IssueCacheLedger()
    older = ledger.open_fetch()
    ledger.record(REMOVE_BLOCK)
    newer = ledger.open_fetch()
    ledger.record(ADD_HUMAN)

    ledger.close_fetch(newer)
    assert ledger.writes_since(older) == (REMOVE_BLOCK, ADD_HUMAN)
    assert ledger.kept_writes == 2

    ledger.close_fetch(older)
    assert ledger.kept_writes == 0


def test_a_newer_read_is_not_handed_a_write_it_already_followed() -> None:
    ledger = IssueCacheLedger()
    older = ledger.open_fetch()
    ledger.record(REMOVE_BLOCK)

    newer = ledger.open_fetch()

    assert ledger.writes_since(newer) == ()
    assert ledger.writes_since(older) == (REMOVE_BLOCK,)


def test_reads_opened_at_the_same_point_each_keep_their_writes() -> None:
    ledger = IssueCacheLedger()
    first = ledger.open_fetch()
    second = ledger.open_fetch()
    ledger.record(REMOVE_BLOCK)

    ledger.close_fetch(first)

    assert ledger.writes_since(second) == (REMOVE_BLOCK,)


def test_closing_a_read_that_is_not_open_fails() -> None:
    ledger = IssueCacheLedger()
    opened = ledger.open_fetch()
    ledger.close_fetch(opened)

    with pytest.raises(ValueError, match="is not open"):
        ledger.close_fetch(opened)
