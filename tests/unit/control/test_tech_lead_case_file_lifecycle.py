"""Durable, cross-client pattern case-file lifecycle tests (#7240)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from issue_orchestrator.adapters.github.pattern_registry import GitHubRefPatternRegistry
from issue_orchestrator.control.tech_lead_case_file_lifecycle import (
    PatternCaseFileLifecycleOwner,
    retirement_comment,
)
from issue_orchestrator.control.tech_lead_case_file_owner import (
    AmbiguousPatternPublicationError,
)
from issue_orchestrator.domain.tech_lead_findings import (
    CASE_FILE_ACTIVE,
    CASE_FILE_DECLINED,
    CASE_FILE_NEEDS_HUMAN,
    CaseFileClassification,
    CaseFileLifecycleTransition,
    PatternObservation,
    PendingCaseFile,
)
from issue_orchestrator.ports.comment_receipt import IssueCommentReceipt
from issue_orchestrator.ports.pattern_registry import PatternReservationState

from tests.unit.adapters.github.test_ref_claim_adapter import FakeGitHubRefClient


def _registry(
    client: FakeGitHubRefClient, claimant: str, now: list[datetime]
) -> GitHubRefPatternRegistry:
    return GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id=claimant,
        lease_seconds=30,
        clock=lambda: now[0],
    )


def _seed(registry: GitHubRefPatternRegistry) -> None:
    pending = PendingCaseFile(
        signature="stale-pattern",
        title="Pattern case file: stale-pattern",
        idempotency_marker="<!-- stale-pattern -->",
        body_observation_id="run:session:A1",
    )
    reserved = registry.reserve(pending)
    registry.finalize(
        signature=pending.signature,
        reservation_id=reserved.entry.reservation_id,
        issue_number=81,
    )


def _transition(
    disposition: str = CASE_FILE_DECLINED,
) -> CaseFileLifecycleTransition:
    return CaseFileLifecycleTransition(
        transition_id="plan-2026-09-10:stale-pattern",
        disposition=disposition,  # type: ignore[arg-type]
        reason="The reviewed promotion was deliberately declined.",
        evidence=("promotion owner/repo#199 closed without a merged PR",),
        recorded_at="2026-09-10T12:00:00+00:00",
    )


class _Repository:
    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []
        self.closures: list[int] = []
        self.fail_comment = False

    def find_issue_comment_receipt(
        self, issue_number: int, *, body: str
    ) -> IssueCommentReceipt | None:
        if (issue_number, body) not in self.comments:
            return None
        return IssueCommentReceipt(
            comment_id="1",
            url="https://example.test/comments/1",
            author_key="app:test",
            body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        )

    def add_comment(self, issue_number: int, body: str) -> str:
        if self.fail_comment:
            raise RuntimeError("ambiguous transport failure")
        self.comments.append((issue_number, body))
        return "https://example.test/comments/1"

    def update_issue_state(self, issue_number: int, state: str) -> None:
        assert state == "closed"
        self.closures.append(issue_number)


def _owner(
    registry: GitHubRefPatternRegistry, repository: _Repository
) -> PatternCaseFileLifecycleOwner:
    return PatternCaseFileLifecycleOwner(
        registry=registry, repository_host=cast(Any, repository)
    )


def test_two_clients_converge_on_one_terminal_disposition() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first = _registry(client, "engine-a", now)
    second = _registry(client, "engine-b", now)
    _seed(first)
    repository = _Repository()

    outcome = _owner(first, repository).retire(
        signature="stale-pattern", transition=_transition()
    )
    replay = _owner(second, repository).retire(
        signature="stale-pattern", transition=_transition()
    )

    assert not outcome.deduplicated
    assert replay.deduplicated
    assert len(repository.comments) == 1
    assert repository.closures == [81]
    entry = second.read(signature="stale-pattern")
    assert entry is not None
    assert entry.disposition == CASE_FILE_DECLINED
    assert entry.lifecycle == (_transition(),)


def test_ambiguous_comment_is_never_reissued_and_later_receipt_recovers() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first = _registry(client, "engine-a", now)
    second = _registry(client, "engine-b", now)
    _seed(first)
    repository = _Repository()
    repository.fail_comment = True

    with pytest.raises(RuntimeError, match="ambiguous transport"):
        _owner(first, repository).retire(
            signature="stale-pattern", transition=_transition()
        )
    now[0] += timedelta(hours=1)
    with pytest.raises(AmbiguousPatternPublicationError):
        _owner(second, repository).retire(
            signature="stale-pattern", transition=_transition()
        )

    repository.fail_comment = False
    repository.comments.append((81, retirement_comment(_transition())))
    recovered = _owner(second, repository).retire(
        signature="stale-pattern", transition=_transition()
    )

    assert not recovered.deduplicated
    assert len(repository.comments) == 1
    assert repository.closures == [81]


def test_restart_after_close_phase_finishes_without_reposting_comment() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    _seed(registry)
    transition = _transition()
    comment = retirement_comment(transition)
    reserved = registry.reserve_retirement(
        signature="stale-pattern", transition=transition, comment=comment
    )
    registry.begin_retirement_publication(
        signature="stale-pattern", reservation_id=reserved.entry.reservation_id
    )
    registry.confirm_retirement_comment(
        signature="stale-pattern", reservation_id=reserved.entry.reservation_id
    )
    repository = _Repository()
    repository.comments.append((81, comment))
    repository.closures.append(81)  # the first process closed, then crashed

    _owner(registry, repository).retire(
        signature="stale-pattern", transition=transition
    )

    assert len(repository.comments) == 1
    assert repository.closures == [81, 81]
    entry = registry.read(signature="stale-pattern")
    assert entry is not None and entry.disposition == CASE_FILE_DECLINED


def test_retry_keeps_first_recorded_timestamp_authoritative() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    _seed(registry)
    repository = _Repository()
    original = _transition()
    _owner(registry, repository).retire(
        signature="stale-pattern", transition=original
    )
    later_retry = CaseFileLifecycleTransition(
        transition_id=original.transition_id,
        disposition=original.disposition,
        reason=original.reason,
        evidence=original.evidence,
        recorded_at="2026-09-11T12:00:00+00:00",
    )

    replay = _owner(registry, repository).retire(
        signature="stale-pattern", transition=later_retry
    )

    assert replay.deduplicated
    entry = registry.read(signature="stale-pattern")
    assert entry is not None and entry.lifecycle == (original,)


def test_terminal_signature_accepts_later_evidence_without_reopening() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    _seed(registry)
    repository = _Repository()
    _owner(registry, repository).retire(
        signature="stale-pattern", transition=_transition()
    )

    admitted = registry.reserve_observation(
        signature="stale-pattern",
        observation=PatternObservation(
            observation_id="run:session:A2", comment="It happened again"
        ),
        classification=CaseFileClassification(),
    )
    assert admitted.state is PatternReservationState.ACQUIRED
    registry.finalize_observation(
        signature="stale-pattern", reservation_id=admitted.entry.reservation_id
    )

    entry = registry.read(signature="stale-pattern")
    assert entry is not None
    assert entry.disposition == CASE_FILE_DECLINED
    assert entry.observation_ids[-1] == "run:session:A2"


@pytest.mark.parametrize("disposition", (CASE_FILE_ACTIVE, CASE_FILE_NEEDS_HUMAN))
def test_nonterminal_reconciliation_records_auditable_outcome(
    disposition: str,
) -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    registry = _registry(client, "engine-a", now)
    _seed(registry)
    transition = _transition(disposition)

    first = _owner(registry, _Repository()).classify(
        signature="stale-pattern", transition=transition
    )
    replay = _owner(registry, _Repository()).classify(
        signature="stale-pattern", transition=transition
    )

    assert first == replay
    assert first.disposition == disposition
    assert first.lifecycle == (transition,)
