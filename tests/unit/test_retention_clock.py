"""Real store authority rejects corrupt clocks before any release callback."""

import sqlite3
from contextlib import closing

import pytest

from issue_orchestrator.domain.validated_work import ValidatedWorkState as State
from .test_validated_work_retention import retained as retained, CUTOFF, NOW


def set_terminal(retained, value):
    with closing(sqlite3.connect(retained.database)) as conn, conn:
        conn.execute("UPDATE validated_work_records SET terminal_at=?", (value,))


@pytest.mark.parametrize(
    "value",
    [
        "!",
        "",
        "2026-09-06",
        "2026-09-06T13:00:00",
        "2026-09-06 13:00:00+00:00",
        "2026-09-06T13:00:00Z",
        "2026-13-06T13:00:00+00:00",
        "2026-09-06T13:00:00+00:00\n",
    ],
)
@pytest.mark.parametrize("state", [State.RECOVERED, State.ABANDONED])
def test_corrupt_terminal_clock_never_selects_or_releases(retained, value, state):
    retained.resolve(state)
    store = retained.open()
    candidate = store.evidence_for_retention(released_before=CUTOFF)[0]
    set_terminal(retained, value)
    before = retained.snapshot()
    for current in (store, retained.open()):
        with pytest.raises(ValueError):
            current.evidence_for_retention(released_before=CUTOFF)
        with pytest.raises(ValueError):
            current.release_evidence_for_retention(
                candidate.evidence_id,
                released_before=CUTOFF,
                released_at=NOW.isoformat(),
            )
    assert retained.snapshot() == before


@pytest.mark.parametrize(
    "terminal,eligible",
    [
        ("2026-08-08T00:00:00+00:00", False),
        ("2026-08-07T20:00:00-04:00", False),
        ("2026-08-08T02:00:00+02:00", False),
        ("2026-08-07T23:59:59.999999+00:00", True),
        ("2026-08-08T01:59:59+02:00", True),
        ("2026-08-07T23:00:00-02:00", False),
        ("2026-09-06T13:00:00+00:00", False),
    ],
)
def test_retention_compares_instants_and_strict_window_boundary(
    retained, terminal, eligible
):
    retained.resolve(State.RECOVERED)
    set_terminal(retained, terminal)
    cutoff = "2026-08-08T00:00:00+00:00"
    store = retained.open()
    assert bool(store.evidence_for_retention(released_before=cutoff)) is eligible
    before = retained.snapshot()
    assert (
        store.release_evidence_for_retention(
            retained.admission.evidence.evidence_id,
            released_before=cutoff,
            released_at=NOW.isoformat(),
        )
        is eligible
    )
    if not eligible:
        assert retained.snapshot() == before


def test_newer_valid_terminal_clock_invalidates_earlier_candidate(retained):
    retained.resolve(State.RECOVERED)
    store = retained.open()
    candidate = store.evidence_for_retention(released_before=CUTOFF)[0]
    set_terminal(retained, NOW.isoformat())
    before = retained.snapshot()
    assert not store.release_evidence_for_retention(
        candidate.evidence_id, released_before=CUTOFF, released_at=NOW.isoformat()
    )
    assert retained.snapshot() == before
