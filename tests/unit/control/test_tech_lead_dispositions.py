"""The failure-investigation disposition owner (#6971).

A completed investigation that names an open recovery tracker is an ANSWER.
These tests pin the two halves that make that answer durable: the apply-time
owner that records the binding, and the sweep-time owner that decides whether
the binding still holds — including every way it must let go, because a
disposition that cannot release is a way to lose an issue forever.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.control.action_results import ActionResultType
from issue_orchestrator.control.actions import RecordTechLeadDispositionAction
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.tech_lead_dispositions import (
    NO_TECH_LEAD_DISPOSITIONS,
    TechLeadDispositionLedger,
    apply_record_tech_lead_disposition,
    build_disposition_ledger,
    disposition_comment,
)
from issue_orchestrator.domain.tech_lead_session import TechLeadDisposition
from issue_orchestrator.ports.tech_lead_authority import (
    InMemoryTechLeadAuthorityStore,
)

BLOCKED = 6410
TRACKER = 6914


def _disposition(
    issue_number: int = BLOCKED, tracker: int = TRACKER
) -> TechLeadDisposition:
    return TechLeadDisposition(
        issue_number=issue_number,
        tracker_issue_number=tracker,
        rationale="Validated work is stranded; recover it, do NOT reset.",
        source_run_id="run-1",
        source_session_name="issue-6410",
        source_action_id="A2",
        recorded_at="2026-08-09T00:00:00+00:00",
    )


class _States:
    """Issue-state reader that records every lookup it is asked to make."""

    def __init__(self, states: dict[int, str | None]) -> None:
        self._states = states
        self.reads: list[int] = []

    def __call__(self, issue_number: int) -> str | None:
        self.reads.append(issue_number)
        return self._states.get(issue_number)


def _ledger(store, states: dict[int, str | None]) -> tuple[TechLeadDispositionLedger, _States]:
    reader = _States(states)
    return TechLeadDispositionLedger(authority=store, issue_state=reader), reader


# ---------------------------------------------------------------------------
# Apply-time owner: the durable binding
# ---------------------------------------------------------------------------


def test_apply_records_the_binding_and_makes_no_github_call() -> None:
    """The wait-state comment is a separate, claim-verified action (#6971)."""
    store = InMemoryTechLeadAuthorityStore()
    disposition = _disposition()

    result = apply_record_tech_lead_disposition(
        RecordTechLeadDispositionAction(
            disposition=disposition, expected=build_expected_for_mutation()
        ),
        authority=store,
    )

    assert result.result_type is ActionResultType.SUCCESS
    assert store.load_disposition(issue_number=BLOCKED) == disposition


def test_apply_without_a_store_fails_loudly_instead_of_pretending() -> None:
    result = apply_record_tech_lead_disposition(
        RecordTechLeadDispositionAction(disposition=_disposition()), authority=None
    )

    assert result.result_type is ActionResultType.FAILURE
    assert "TechLeadAuthorityStore" in (result.error or "")


def test_a_later_investigation_supersedes_the_recorded_tracker() -> None:
    """The row means "the latest conclusion" — freezing it on a stale tracker
    would park the issue on something that may already be closed."""
    store = InMemoryTechLeadAuthorityStore()
    store.record_disposition(disposition=_disposition(tracker=TRACKER))

    store.record_disposition(disposition=_disposition(tracker=6959))

    recorded = store.load_disposition(issue_number=BLOCKED)
    assert recorded is not None
    assert recorded.tracker_issue_number == 6959


def test_the_action_refuses_to_exist_without_a_disposition() -> None:
    with pytest.raises(ValueError, match="requires the disposition"):
        RecordTechLeadDispositionAction()


def test_the_comment_names_the_tracker_and_the_release_condition() -> None:
    """The comment is the operator's only on-issue explanation of the parking."""
    comment = disposition_comment(_disposition())

    assert f"#{TRACKER}" in comment
    assert "Validated work is stranded" in comment
    assert "closes" in comment
    assert "A2" in comment


# ---------------------------------------------------------------------------
# Sweep-time owner: ownership, and every way it releases
# ---------------------------------------------------------------------------


def test_an_open_tracker_owns_the_diagnosed_issue() -> None:
    store = InMemoryTechLeadAuthorityStore()
    store.record_disposition(disposition=_disposition())
    ledger, _ = _ledger(store, {TRACKER: "open"})

    assert ledger.owned_issue_numbers() == frozenset({BLOCKED})
    assert store.load_disposition(issue_number=BLOCKED) is not None


def test_a_closed_tracker_releases_the_issue_back_to_the_sweep() -> None:
    """The binding IS the release condition: the wait state is over."""
    store = InMemoryTechLeadAuthorityStore()
    store.record_disposition(disposition=_disposition())
    ledger, _ = _ledger(store, {TRACKER: "closed"})

    assert ledger.owned_issue_numbers() == frozenset()
    assert store.load_disposition(issue_number=BLOCKED) is None


def test_a_tracker_that_does_not_exist_releases_the_issue() -> None:
    """Agent-named tracker numbers are untrusted; a bogus one must not park
    an issue silently and forever."""
    store = InMemoryTechLeadAuthorityStore()
    store.record_disposition(disposition=_disposition())
    ledger, _ = _ledger(store, {})

    assert ledger.owned_issue_numbers() == frozenset()
    assert store.load_disposition(issue_number=BLOCKED) is None


def test_an_unreadable_tracker_releases_rather_than_parks() -> None:
    """Failing OPEN would let one flaky read park an issue indefinitely;
    failing closed costs at most one redundant investigation."""
    store = InMemoryTechLeadAuthorityStore()
    store.record_disposition(disposition=_disposition())

    def exploding(_issue_number: int) -> str | None:
        raise RuntimeError("GitHub said no")

    ledger = TechLeadDispositionLedger(authority=store, issue_state=exploding)

    assert ledger.owned_issue_numbers() == frozenset()
    assert store.load_disposition(issue_number=BLOCKED) is None


def test_recovery_releases_the_disposition() -> None:
    """A recovered issue's row must not survive to park a LATER incident."""
    store = InMemoryTechLeadAuthorityStore()
    store.record_disposition(disposition=_disposition())
    ledger, _ = _ledger(store, {TRACKER: "open"})

    ledger.release(frozenset({BLOCKED}))

    assert store.load_disposition(issue_number=BLOCKED) is None
    assert ledger.owned_issue_numbers() == frozenset()


def test_releasing_an_unparked_issue_is_a_no_op() -> None:
    store = InMemoryTechLeadAuthorityStore()
    store.record_disposition(disposition=_disposition())
    ledger, _ = _ledger(store, {TRACKER: "open"})

    ledger.release(frozenset({999}))

    assert store.load_disposition(issue_number=BLOCKED) is not None


def test_one_read_per_distinct_tracker_however_many_issues_it_owns() -> None:
    """GitHub API discipline: the ledger is small, and shared trackers are the
    common case (a recovery mechanism owns several stranded issues)."""
    store = InMemoryTechLeadAuthorityStore()
    store.record_disposition(disposition=_disposition(issue_number=6410))
    store.record_disposition(disposition=_disposition(issue_number=6411))
    ledger, reader = _ledger(store, {TRACKER: "open"})

    assert ledger.owned_issue_numbers() == frozenset({6410, 6411})
    assert reader.reads == [TRACKER]


def test_an_empty_ledger_makes_no_reads_at_all() -> None:
    store = InMemoryTechLeadAuthorityStore()
    ledger, reader = _ledger(store, {TRACKER: "open"})

    assert ledger.owned_issue_numbers() == frozenset()
    assert reader.reads == []


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_without_an_authority_store_nothing_is_disposition_owned() -> None:
    class _Host:
        def get_issue_state(self, issue_number: int, repo: str | None = None):
            raise AssertionError("must not read GitHub without a ledger")

    ledger = build_disposition_ledger(None, _Host())

    assert ledger is NO_TECH_LEAD_DISPOSITIONS
    assert ledger.owned_issue_numbers() == frozenset()
    ledger.release(frozenset({BLOCKED}))


def test_with_a_store_the_ledger_reads_tracker_state_through_the_host() -> None:
    store = InMemoryTechLeadAuthorityStore()
    store.record_disposition(disposition=_disposition())

    class _Host:
        def __init__(self) -> None:
            self.reads: list[int] = []

        def get_issue_state(self, issue_number: int, repo: str | None = None):
            self.reads.append(issue_number)
            return "open"

    host = _Host()
    ledger = build_disposition_ledger(store, host)

    assert ledger.owned_issue_numbers() == frozenset({BLOCKED})
    assert host.reads == [TRACKER]
