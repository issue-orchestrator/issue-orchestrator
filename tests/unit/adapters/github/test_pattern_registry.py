"""Contract tests for the shared GitHub-ref pattern registry (#6789)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from issue_orchestrator.adapters.github.pattern_registry import (
    GitHubRefPatternRegistry,
    PATTERN_REGISTRY_REF_KEY,
    PATTERN_REGISTRY_REF_PREFIX,
)
from issue_orchestrator.domain.tech_lead_findings import (
    CaseFileClassification,
    PatternClassificationConflictError,
    PatternObservation,
    PendingCaseFile,
)
from issue_orchestrator.ports.pattern_registry import (
    PatternRegistryError,
    PatternReservationState,
)

from .test_ref_claim_adapter import FakeGitHubRefClient


def _pending(signature: str, observation: str = "run:session:A1") -> PendingCaseFile:
    return PendingCaseFile(
        signature=signature,
        title=f"Pattern case file: {signature}",
        idempotency_marker=f"<!-- marker:{signature} -->",
        body_observation_id=observation,
        fix_class="code",
        area="runtime",
        diagnosis="Retry ownership can be stranded.",
    )


def _registry(
    client: FakeGitHubRefClient,
    claimant: str,
    now: list[datetime],
) -> GitHubRefPatternRegistry:
    return GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id=claimant,
        lease_seconds=30,
        clock=lambda: now[0],
    )


def _observation(identity: str) -> PatternObservation:
    return PatternObservation(
        observation_id=identity,
        comment=f"Evidence for {identity}\n\n<!-- observation:{identity} -->",
    )


def _record(
    registry: GitHubRefPatternRegistry,
    *,
    identity: str,
    classification: CaseFileClassification,
) -> bool:
    admitted = registry.reserve_observation(
        signature="stuck-retry",
        observation=_observation(identity),
        classification=classification,
    )
    assert admitted.state is PatternReservationState.ACQUIRED
    return registry.finalize_observation(
        signature="stuck-retry",
        reservation_id=admitted.entry.reservation_id,
    )


def test_two_clients_converge_on_one_reservation_and_case_file() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first = _registry(client, "engine-a", now)
    second = _registry(client, "engine-b", now)

    won = first.reserve(_pending("stuck-retry"))
    lost = second.reserve(_pending("stuck-retry", "run:session:A2"))

    assert won.state is PatternReservationState.ACQUIRED
    assert lost.state is PatternReservationState.HELD
    committed = first.finalize(
        signature="stuck-retry",
        reservation_id=won.entry.reservation_id,
        issue_number=81,
    )
    assert (
        second.reserve(_pending("stuck-retry")).state
        is PatternReservationState.COMMITTED
    )
    assert committed.issue_number == 81


def test_stale_reservation_requires_exact_takeover_token() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first = _registry(client, "engine-a", now)
    second = _registry(client, "engine-b", now)
    stale = first.reserve(_pending("stuck-retry"))
    now[0] += timedelta(seconds=31)

    recovery = second.reserve(_pending("stuck-retry", "run:session:A2"))
    assert recovery.state is PatternReservationState.RECOVERABLE
    held = second.take_over(
        stale_reservation_id="wrong",
        pending=_pending("stuck-retry", "run:session:A2"),
    )
    assert held.state is PatternReservationState.HELD
    acquired = second.take_over(
        stale_reservation_id=stale.entry.reservation_id,
        pending=_pending("stuck-retry", "run:session:A2"),
    )
    assert acquired.state is PatternReservationState.ACQUIRED
    assert acquired.entry.claimant_id == "engine-b"


def test_restarted_same_claimant_must_recover_its_existing_reservation() -> None:
    """A stable claimant name cannot substitute for the reservation token."""
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    before_restart = _registry(client, "engine-a", now)
    reserved = before_restart.reserve(_pending("stuck-retry"))

    after_restart = _registry(client, "engine-a", now)
    recovered = after_restart.reserve(_pending("stuck-retry"))

    assert reserved.state is PatternReservationState.ACQUIRED
    assert recovered.state is PatternReservationState.RECOVERABLE
    assert recovered.entry.reservation_id == reserved.entry.reservation_id


def test_stale_creator_cannot_renew_after_exact_takeover() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first = _registry(client, "engine-a", now)
    second = _registry(client, "engine-b", now)
    stale = first.reserve(_pending("stuck-retry"))
    now[0] += timedelta(seconds=31)
    recovery = second.reserve(_pending("stuck-retry", "run:session:A2"))
    takeover = second.take_over(
        stale_reservation_id=recovery.entry.reservation_id,
        pending=_pending("stuck-retry", "run:session:A2"),
    )

    fenced = first.begin_creation_publication(
        signature="stuck-retry", reservation_id=stale.entry.reservation_id
    )

    assert takeover.state is PatternReservationState.ACQUIRED
    assert fenced.state is PatternReservationState.HELD


def test_started_creation_token_cannot_admit_a_second_publication() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    reserved = registry.reserve(_pending("stuck-retry"))

    started = registry.begin_creation_publication(
        signature="stuck-retry", reservation_id=reserved.entry.reservation_id
    )
    replay = registry.begin_creation_publication(
        signature="stuck-retry", reservation_id=reserved.entry.reservation_id
    )

    assert started.state is PatternReservationState.ACQUIRED
    assert replay.state is PatternReservationState.PUBLISHING


def test_interrupted_finalize_is_recoverable_from_original_pending_payload() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first = _registry(client, "engine-a", now)
    second = _registry(client, "engine-b", now)
    reserved = first.reserve(_pending("stuck-retry"))
    now[0] += timedelta(seconds=31)

    recovery = second.reserve(_pending("stuck-retry", "run:session:A2"))
    committed = second.finalize(
        signature="stuck-retry",
        reservation_id=recovery.entry.reservation_id,
        issue_number=81,
    )

    assert reserved.entry.pending == recovery.entry.pending
    assert committed.observation_ids == ("run:session:A1",)
    assert committed.classification.fix_class == "code"


def test_concurrent_evidence_retries_preserve_both_observations() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first = _registry(client, "engine-a", now)
    second = _registry(client, "engine-b", now)
    reserved = first.reserve(_pending("stuck-retry"))
    first.finalize(
        signature="stuck-retry",
        reservation_id=reserved.entry.reservation_id,
        issue_number=81,
    )
    client.conflict_updates_remaining = 1

    classification = CaseFileClassification(fix_class="code", area="runtime")
    assert _record(first, identity="run:session:A2", classification=classification)
    assert _record(second, identity="run:session:A3", classification=classification)
    replay = second.reserve_observation(
        signature="stuck-retry",
        observation=_observation("run:session:A3"),
        classification=classification,
    )
    assert replay.state is PatternReservationState.COMMITTED
    entry = second.read(signature="stuck-retry")
    assert entry is not None
    assert entry.observation_ids == (
        "run:session:A1",
        "run:session:A2",
        "run:session:A3",
    )


def test_observation_reservation_serializes_publication_and_classification() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first = _registry(client, "engine-a", now)
    second = _registry(client, "engine-b", now)
    created = first.reserve(_pending("stuck-retry"))
    first.finalize(
        signature="stuck-retry",
        reservation_id=created.entry.reservation_id,
        issue_number=81,
    )
    admitted = first.reserve_observation(
        signature="stuck-retry",
        observation=_observation("run:session:A2"),
        classification=CaseFileClassification(fix_class="code", area="runtime"),
    )

    duplicate = second.reserve_observation(
        signature="stuck-retry",
        observation=_observation("run:session:A2"),
        classification=CaseFileClassification(fix_class="code", area="runtime"),
    )
    with pytest.raises(PatternClassificationConflictError):
        second.reserve_observation(
            signature="stuck-retry",
            observation=_observation("run:session:A3"),
            classification=CaseFileClassification(fix_class="human", area="runtime"),
        )

    assert admitted.state is PatternReservationState.ACQUIRED
    assert duplicate.state is PatternReservationState.HELD
    assert first.finalize_observation(
        signature="stuck-retry",
        reservation_id=admitted.entry.reservation_id,
    )
    assert (
        second.reserve_observation(
            signature="stuck-retry",
            observation=_observation("run:session:A2"),
            classification=CaseFileClassification(fix_class="code", area="runtime"),
        ).state
        is PatternReservationState.COMMITTED
    )


def test_same_claimant_cannot_take_over_live_observation_token() -> None:
    """A process name is not unique authority for overlapping operations."""
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first = _registry(client, "engine-a", now)
    retry = _registry(client, "engine-a", now)
    created = first.reserve(_pending("stuck-retry"))
    first.finalize(
        signature="stuck-retry",
        reservation_id=created.entry.reservation_id,
        issue_number=81,
    )
    admitted = first.reserve_observation(
        signature="stuck-retry",
        observation=_observation("run:session:A2"),
        classification=CaseFileClassification(),
    )

    observed = retry.reserve_observation(
        signature="stuck-retry",
        observation=_observation("run:session:A2"),
        classification=CaseFileClassification(),
    )
    takeover = retry.take_over_observation(
        signature="stuck-retry",
        stale_reservation_id=admitted.entry.reservation_id,
    )

    assert observed.state is PatternReservationState.HELD
    assert takeover.state is PatternReservationState.HELD
    assert takeover.entry.reservation_id == admitted.entry.reservation_id


def test_different_signatures_retain_independent_records() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)

    first = registry.reserve(_pending("pattern-a"))
    second = registry.reserve(_pending("pattern-b", "run:session:B1"))
    registry.finalize(
        signature="pattern-a",
        reservation_id=first.entry.reservation_id,
        issue_number=81,
    )
    registry.finalize(
        signature="pattern-b",
        reservation_id=second.entry.reservation_id,
        issue_number=82,
    )

    assert [
        (entry.signature, entry.issue_number) for entry in registry.list_entries()
    ] == [
        ("pattern-a", 81),
        ("pattern-b", 82),
    ]


@pytest.mark.parametrize(
    "body",
    [
        '{"version":99,"entries":[]}',
        '{"version":true,"entries":[]}',
        '{"version":1,"entries":{}}',
        '{"version":1,"entries":[],"entries":[]}',
        '{"version":1,"entries":[],"extra":true}',
    ],
)
def test_unreadable_registry_fails_closed(body: str) -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    registry.reserve(_pending("pattern-a"))
    ref = f"{PATTERN_REGISTRY_REF_PREFIX}/{PATTERN_REGISTRY_REF_KEY}"
    client.commits[client.refs[ref]]["message"] = (
        "issue-orchestrator tech-lead pattern registry\n\n" + body
    )

    with pytest.raises(PatternRegistryError, match="unreadable"):
        registry.list_entries()


def test_existing_blank_or_unmarked_registry_fails_closed() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    registry.reserve(_pending("pattern-a"))
    ref = f"{PATTERN_REGISTRY_REF_PREFIX}/{PATTERN_REGISTRY_REF_KEY}"

    for message in ("", 'wrong marker\n\n{"version":1,"entries":[]}'):
        client.commits[client.refs[ref]]["message"] = message
        with pytest.raises(PatternRegistryError, match="unreadable"):
            registry.list_entries()


def test_invalid_publication_state_fails_closed() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    registry.reserve(_pending("pattern-a"))
    ref = f"{PATTERN_REGISTRY_REF_PREFIX}/{PATTERN_REGISTRY_REF_KEY}"
    message = client.commits[client.refs[ref]]["message"]
    client.commits[client.refs[ref]]["message"] = message.replace(
        '"publication_started_at":null', '"publication_started_at":true'
    )

    with pytest.raises(PatternRegistryError, match="unreadable"):
        registry.list_entries()
