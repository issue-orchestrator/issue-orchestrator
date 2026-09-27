"""The action liveness policy's arithmetic and fingerprint (#7350)."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import pytest

from issue_orchestrator.domain.action_liveness import (
    ActionIdentity,
    ActionOutcome,
    Admission,
    LivenessKey,
    LivenessPolicy,
    OutcomeKind,
    admission,
    fact_fingerprint,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
KEY = LivenessKey(ActionIdentity("issue:410", "remove_label"), "f" * 32, 410)
POLICY = LivenessPolicy(
    max_attempts=4,
    base_backoff=timedelta(minutes=1),
    max_backoff=timedelta(minutes=5),
    declared_wait_bound=timedelta(hours=2),
)


def _fail_repeatedly(outcome: ActionOutcome, times: int, *, step=timedelta(hours=1)):
    row = None
    now = NOW
    for _ in range(times):
        row = POLICY.after(row, KEY, outcome, now)
        now += step
    return row


class TestTransient:
    def test_backoff_doubles_and_caps(self) -> None:
        row = POLICY.after(None, KEY, ActionOutcome.transient("503"), NOW)
        assert row is not None
        assert (row.attempts, row.next_attempt_at) == (1, NOW + timedelta(minutes=1))
        row = POLICY.after(row, KEY, ActionOutcome.transient("503"), NOW)
        assert row.next_attempt_at == NOW + timedelta(minutes=2)
        row = POLICY.after(row, KEY, ActionOutcome.transient("503"), NOW)
        assert row.next_attempt_at == NOW + timedelta(minutes=4)
        assert POLICY.backoff(10) == timedelta(minutes=5)
        assert POLICY.backoff(10_000) == timedelta(minutes=5)

    def test_parks_once_the_budget_is_spent(self) -> None:
        row = _fail_repeatedly(ActionOutcome.transient("503"), POLICY.max_attempts - 1)
        assert row is not None and not row.parked
        row = POLICY.after(row, KEY, ActionOutcome.transient("503"), NOW)
        assert row is not None and row.parked
        assert row.attempts == POLICY.max_attempts
        assert "4 attempts failed with unchanged facts; last: 503" == row.last_reason
        assert row.first_failed_at == NOW

    def test_a_declared_wait_spends_nothing_within_the_bound(self) -> None:
        reset = NOW + timedelta(minutes=40)
        row = None
        for _ in range(50):
            row = POLICY.after(row, KEY, ActionOutcome.transient("rate limited", reset), NOW)
        assert row is not None and not row.parked
        assert (row.attempts, row.next_attempt_at) == (0, reset)

    def test_a_declared_wait_past_the_bound_spends_and_parks(self) -> None:
        row = POLICY.after(None, KEY, ActionOutcome.transient("rate limited", NOW), NOW)
        later = NOW + POLICY.declared_wait_bound
        for _ in range(POLICY.max_attempts):
            reset = later + timedelta(minutes=30)
            row = POLICY.after(row, KEY, ActionOutcome.transient("rate limited", reset), later)
        assert row is not None and row.parked

    def test_past_the_bound_the_ladder_sets_the_pace_not_the_reset(self) -> None:
        row = POLICY.after(None, KEY, ActionOutcome.transient("x"), NOW)
        late = NOW + POLICY.declared_wait_bound
        reset = late + timedelta(days=30)
        row = POLICY.after(row, KEY, ActionOutcome.transient("rate limited", reset), late)
        assert row is not None and row.attempts == 2
        assert row.next_attempt_at == late + POLICY.backoff(2)

    def test_a_declared_wait_never_outlives_the_bound(self) -> None:
        """A reset declared a month ahead waits only until the bound (review r5)."""
        reset = NOW + timedelta(days=30)
        row = POLICY.after(None, KEY, ActionOutcome.transient("rate limited", reset), NOW)
        assert row is not None and row.attempts == 0
        assert row.next_attempt_at == NOW + POLICY.declared_wait_bound
        now = row.next_attempt_at
        for _ in range(POLICY.max_attempts):
            row = POLICY.after(row, KEY, ActionOutcome.transient("rate limited", reset), now)
            assert row is not None
            now = row.next_attempt_at or now
        assert row.parked


@pytest.mark.parametrize(
    "outcome",
    [ActionOutcome.permanent("422 unprocessable"), ActionOutcome.needs_human("paused")],
)
def test_permanent_and_needs_human_park_on_first_occurrence(outcome) -> None:
    row = POLICY.after(None, KEY, outcome, NOW)
    assert row is not None and row.parked and row.attempts == 1
    assert row.last_outcome is outcome.kind
    assert admission(row, NOW + timedelta(days=365)) is Admission.PARKED


@pytest.mark.parametrize(
    "outcome",
    [
        ActionOutcome.transient("rate limited", NOW + timedelta(minutes=30)),
        ActionOutcome.transient("still broken"),
        ActionOutcome.permanent("refused"),
    ],
    ids=["declared-wait", "transient", "permanent"],
)
def test_a_park_ends_only_by_success(outcome) -> None:
    """An attempt run despite a park (an operator's explicit recovery) that
    does not succeed leaves it parked with its escalation, so its block stays
    accounted for (review B r6)."""
    parked = POLICY.after(None, KEY, ActionOutcome.permanent("stuck"), NOW)
    assert parked is not None and parked.parked
    escalated = replace(parked, escalated=True, explained=True, escalation_attempts=1,
                        escalation_attempted_at=NOW)

    row = POLICY.after(escalated, KEY, outcome, NOW + timedelta(minutes=5))

    assert row is not None and row.parked
    assert (row.escalated, row.explained, row.escalation_attempts) == (True, True, 1)
    assert row.first_failed_at == NOW


def test_done_leaves_no_row() -> None:
    previous = POLICY.after(None, KEY, ActionOutcome.transient("x"), NOW)
    assert POLICY.after(previous, KEY, ActionOutcome.done(), NOW) is None


def test_admission_follows_the_backoff() -> None:
    row = POLICY.after(None, KEY, ActionOutcome.transient("x"), NOW)
    assert admission(None, NOW) is Admission.ADMIT
    assert admission(row, NOW) is Admission.BACKING_OFF
    assert admission(row, NOW + timedelta(minutes=1)) is Admission.ADMIT


class TestOutcomeVocabulary:
    def test_retry_at_only_on_transient(self) -> None:
        with pytest.raises(ValueError):
            ActionOutcome(OutcomeKind.PERMANENT, "x", NOW)

    def test_failures_need_a_reason(self) -> None:
        with pytest.raises(ValueError):
            ActionOutcome.transient("  ")

    def test_retry_at_must_be_aware(self) -> None:
        with pytest.raises(ValueError):
            ActionOutcome.transient("x", datetime(2026, 1, 1))


class _Colour(Enum):
    RED = "red"


@dataclass(frozen=True)
class _Facts:
    labels: frozenset[str]
    colour: _Colour
    when: datetime
    where: Path


class TestFingerprint:
    def test_equal_facts_equal_fingerprints_regardless_of_set_order(self) -> None:
        one = _Facts(frozenset({"a", "b", "c"}), _Colour.RED, NOW, Path("/x"))
        two = _Facts(frozenset({"c", "a", "b"}), _Colour.RED, NOW, Path("/x"))
        assert fact_fingerprint(one) == fact_fingerprint(two)

    def test_any_changed_fact_changes_it(self) -> None:
        base = _Facts(frozenset({"a"}), _Colour.RED, NOW, Path("/x"))
        changed = _Facts(frozenset({"a", "io:needs-reconcile"}), _Colour.RED, NOW, Path("/x"))
        assert fact_fingerprint(base) != fact_fingerprint(changed)

    def test_unknown_types_fail_loudly(self) -> None:
        with pytest.raises(TypeError):
            fact_fingerprint({"x": object()})

    def test_stable_across_processes(self) -> None:
        """A restart must not reset every budget: no hash-seed dependence."""
        code = (
            "from issue_orchestrator.domain.action_liveness import fact_fingerprint;"
            "print(fact_fingerprint({'labels': frozenset({'a','b','c','d','e'})}))"
        )
        env = dict(os.environ)
        prints = set()
        for seed in ("1", "2", "3"):
            env["PYTHONHASHSEED"] = seed
            prints.add(
                subprocess.run(
                    [sys.executable, "-c", code], env=env, capture_output=True,
                    text=True, check=True,
                ).stdout.strip()
            )
        assert len(prints) == 1


def test_a_wait_spends_nothing_never_parks_and_is_paced() -> None:
    row = POLICY.after(None, KEY, ActionOutcome.transient("x"), NOW)
    for _ in range(50):
        row = POLICY.after(row, KEY, ActionOutcome.waiting("runtime active"), NOW)
    assert row is not None and not row.parked and row.attempts == 1
    assert row.last_outcome is OutcomeKind.WAITING
    assert row.next_attempt_at == NOW + POLICY.max_backoff
