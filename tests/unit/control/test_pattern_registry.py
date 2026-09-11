"""Local/shared pattern registry replication tests (#6789)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.adapters.github.pattern_registry import GitHubRefPatternRegistry
from issue_orchestrator.adapters.github.github_adapter import GitHubAdapter
from issue_orchestrator.control.pattern_registry import MirroredPatternCaseFileRegistry
from issue_orchestrator.control.tech_lead_case_file_owner import (
    AmbiguousPatternPublicationError,
    CaseFileState,
    PatternCaseFileOwner,
)
from issue_orchestrator.control.actions import CreateTechLeadCaseFileIssueAction
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.domain.tech_lead_findings import (
    CaseFileClassification,
    PatternObservation,
    PendingCaseFile,
)
from issue_orchestrator.domain.tech_lead_session import TechLeadCreationOrigin
from issue_orchestrator.ports.pattern_registry import (
    PatternRegistryError,
    PatternReservationState,
)
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from issue_orchestrator.entrypoints.bootstrap_tech_lead import create_pattern_registry
from issue_orchestrator.infra.config import Config

from tests.unit.adapters.github.test_ref_claim_adapter import FakeGitHubRefClient


def _pending(observation: str) -> PendingCaseFile:
    return PendingCaseFile(
        signature="stuck-retry",
        title="Pattern case file: stuck-retry",
        idempotency_marker="<!-- marker:stuck-retry -->",
        body_observation_id=observation,
        fix_class="code",
        area="runtime",
        diagnosis="A retry owner can be stranded.",
    )


def _action() -> CreateTechLeadCaseFileIssueAction:
    observation = PatternObservation(
        observation_id="run:a:A1", comment="first observation"
    )
    return CreateTechLeadCaseFileIssueAction(
        title="Pattern case file: stuck-retry",
        body="body\n\n<!-- marker:stuck-retry -->",
        labels=("tech-lead-observation",),
        pattern_signature="stuck-retry",
        idempotency_marker="<!-- marker:stuck-retry -->",
        observations=(observation,),
        origin=TechLeadCreationOrigin.derived_from_anchor(7),
        expected=build_expected_for_mutation(),
    )


def _mirrored(
    client: FakeGitHubRefClient,
    local: InMemoryTechLeadAuthorityStore,
    claimant: str,
) -> MirroredPatternCaseFileRegistry:
    shared = GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id=claimant,
        lease_seconds=30,
        clock=lambda: datetime(2026, 9, 10, tzinfo=timezone.utc),
    )
    return MirroredPatternCaseFileRegistry(
        shared=shared, local=local, claimant_id=claimant
    )


def test_new_client_reconstructs_existing_mapping_and_evidence() -> None:
    client = FakeGitHubRefClient()
    first_local = InMemoryTechLeadAuthorityStore()
    first_local.record_pattern(
        signature="stuck-retry",
        issue_number=81,
        observation_id="run:a:A1",
        fix_class="code",
        area="runtime",
        diagnosis="A retry owner can be stranded.",
    )
    first_local.note_pattern_observation(
        signature="stuck-retry",
        observation_id="run:a:A2",
        fix_class="code",
        area="runtime",
    )
    _mirrored(client, first_local, "engine-a").synchronize()

    second_local = InMemoryTechLeadAuthorityStore()
    second = _mirrored(client, second_local, "engine-b")
    second.synchronize()

    evidence = second_local.load_pattern_evidence(signature="stuck-retry")
    assert evidence is not None
    assert evidence.case_file_issue_number == 81
    assert evidence.observation_count == 2
    assert second_local.list_pattern_observation_ids(signature="stuck-retry") == (
        "run:a:A1",
        "run:a:A2",
    )


def test_remote_evidence_commit_hydrates_local_replica() -> None:
    client = FakeGitHubRefClient()
    first_local = InMemoryTechLeadAuthorityStore()
    first = _mirrored(client, first_local, "engine-a")
    reserved = first.reserve(_pending("run:a:A1"))
    first.finalize(
        signature="stuck-retry",
        reservation_id=reserved.entry.reservation_id,
        issue_number=81,
    )

    second_local = InMemoryTechLeadAuthorityStore()
    second = _mirrored(client, second_local, "engine-b")
    admitted = second.reserve_observation(
        signature="stuck-retry",
        observation=PatternObservation(
            observation_id="run:b:A1", comment="observed again"
        ),
        classification=CaseFileClassification(fix_class="code", area="runtime"),
        issue_number=81,
    )
    assert second.finalize_observation(
        signature="stuck-retry",
        reservation_id=admitted.entry.reservation_id,
    )

    evidence = second_local.load_pattern_evidence(signature="stuck-retry")
    assert evidence is not None and evidence.observation_count == 2


def test_pre_registry_pending_payload_is_published_before_newer_observation() -> None:
    client = FakeGitHubRefClient()
    local = InMemoryTechLeadAuthorityStore()
    original = _pending("run:a:A1")
    local.record_pending_case_file(pending=original)
    registry = _mirrored(client, local, "engine-a")

    outcome = registry.reserve(_pending("run:a:A2"))

    assert outcome.state is PatternReservationState.RECOVERABLE
    assert outcome.entry.pending == original


def test_shared_commit_retires_a_stranded_local_creation_intent() -> None:
    client = FakeGitHubRefClient()
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    pending = _pending("run:a:A1")
    local = InMemoryTechLeadAuthorityStore()
    local.record_pending_case_file(pending=pending)
    shared = GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id="engine-a",
        lease_seconds=30,
        clock=lambda: now,
    )
    reserved = shared.reserve(pending)
    shared.finalize(
        signature=pending.signature,
        reservation_id=reserved.entry.reservation_id,
        issue_number=81,
    )
    mirrored = MirroredPatternCaseFileRegistry(
        shared=shared, local=local, claimant_id="engine-a"
    )

    entry = mirrored.read(signature=pending.signature)

    assert entry is not None and entry.issue_number == 81
    assert local.load_pending_case_file(signature=pending.signature) is None


def test_expired_observation_recovery_fences_old_publisher_and_posts_once() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first_registry = GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id="engine-a",
        lease_seconds=30,
        clock=lambda: now[0],
    )
    second_registry = GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id="engine-b",
        lease_seconds=30,
        clock=lambda: now[0],
    )
    created = first_registry.reserve(_pending("run:a:A1"))
    first_registry.finalize(
        signature="stuck-retry",
        reservation_id=created.entry.reservation_id,
        issue_number=81,
    )
    observation = PatternObservation(
        observation_id="run:a:A2", comment="observed again\n\n<!-- obs:a2 -->"
    )
    admitted = first_registry.reserve_observation(
        signature="stuck-retry",
        observation=observation,
        classification=CaseFileClassification(fix_class="code", area="runtime"),
        issue_number=81,
    )
    now[0] += timedelta(seconds=31)
    repository = MagicMock()
    published = False
    receipt = object()

    def find_receipt(_number, *, body):
        assert body == observation.comment
        return receipt if published else None

    def add_comment(_number, body):
        nonlocal published
        assert body == observation.comment
        published = True
        return "comment"

    repository.find_issue_comment_receipt.side_effect = find_receipt
    owner = PatternCaseFileOwner(
        registry=second_registry,
        repository_host=repository,
        add_comment=add_comment,
        before_write=lambda: None,
    )

    outcome = owner.append_observations(
        signature="stuck-retry",
        issue_number=81,
        observations=(observation,),
        fix_class="code",
        area="runtime",
        diagnosis="",
    )
    fenced = first_registry.begin_observation_publication(
        signature="stuck-retry",
        reservation_id=admitted.entry.reservation_id,
    )

    assert outcome.recorded == 1
    assert repository.find_issue_comment_receipt.call_count == 2
    assert fenced.state is PatternReservationState.COMMITTED
    entry = second_registry.read(signature="stuck-retry")
    assert entry is not None and entry.observation_ids[-1] == "run:a:A2"


def test_live_peer_observation_reservation_prevents_comment_publication() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first = GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id="engine-a",
        lease_seconds=30,
        clock=lambda: now[0],
    )
    created = first.reserve(_pending("run:a:A1"))
    first.finalize(
        signature="stuck-retry",
        reservation_id=created.entry.reservation_id,
        issue_number=81,
    )
    observation = PatternObservation(
        observation_id="run:a:A2", comment="observed again"
    )
    first.reserve_observation(
        signature="stuck-retry",
        observation=observation,
        classification=CaseFileClassification(),
        issue_number=81,
    )
    second = GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id="engine-b",
        lease_seconds=30,
        clock=lambda: now[0],
    )
    repository = MagicMock()
    owner = PatternCaseFileOwner(
        registry=second,
        repository_host=repository,
        add_comment=repository.add_comment,
        before_write=lambda: None,
    )

    with pytest.raises(PatternRegistryError, match="being published"):
        owner.append_observations(
            signature="stuck-retry",
            issue_number=81,
            observations=(observation,),
            fix_class="",
            area="",
            diagnosis="",
        )

    repository.find_issue_comment_receipt.assert_not_called()
    repository.add_comment.assert_not_called()


def test_creation_owner_revalidates_exact_token_after_takeover() -> None:
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first_registry = GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id="engine-a",
        lease_seconds=30,
        clock=lambda: now[0],
    )
    second_registry = GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id="engine-b",
        lease_seconds=30,
        clock=lambda: now[0],
    )
    repository = MagicMock()
    repository.find_issue_by_marker.return_value = None
    action = _action()
    first_owner = PatternCaseFileOwner(
        registry=first_registry,
        repository_host=repository,
        add_comment=repository.add_comment,
        before_write=lambda: None,
    )
    second_owner = PatternCaseFileOwner(
        registry=second_registry,
        repository_host=repository,
        add_comment=repository.add_comment,
        before_write=lambda: None,
    )
    assert first_owner.resolve(action).issue_number is None
    now[0] += timedelta(seconds=31)
    assert second_owner.resolve(action).issue_number is None

    with pytest.raises(PatternRegistryError, match="changed before publication"):
        first_owner.begin(action)

    repository.create_issue.assert_not_called()


def test_unresolved_issue_write_cannot_be_reissued_after_lease_expiry() -> None:
    """A second client entering during the HTTP call cannot create a duplicate."""
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first_registry = GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id="engine-a",
        lease_seconds=30,
        clock=lambda: now[0],
    )
    second_registry = GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id="engine-b",
        lease_seconds=30,
        clock=lambda: now[0],
    )
    created: list[int] = []
    repository = MagicMock()
    repository.find_issue_by_marker.side_effect = lambda **_kwargs: (
        created[0] if created else None
    )
    first_owner = PatternCaseFileOwner(
        registry=first_registry,
        repository_host=repository,
        add_comment=repository.add_comment,
        before_write=lambda: None,
    )
    second_owner = PatternCaseFileOwner(
        registry=second_registry,
        repository_host=repository,
        add_comment=repository.add_comment,
        before_write=lambda: None,
    )
    action = _action()

    assert first_owner.resolve(action).state is CaseFileState.ABSENT
    first_owner.begin(action)

    def delayed_create() -> int:
        now[0] += timedelta(seconds=31)
        with pytest.raises(
            AmbiguousPatternPublicationError, match="prevent a duplicate issue"
        ):
            second_owner.resolve(action)
        created.append(81)
        return 81

    repository.create_issue.side_effect = delayed_create
    issue_number = repository.create_issue()
    # Simulate the first process crashing after the HTTP effect, before finalize.
    recovered = second_owner.resolve(action)

    assert issue_number == 81
    repository.create_issue.assert_called_once_with()
    assert recovered == type(recovered)(CaseFileState.RECOVERED, 81)
    entry = first_registry.read(signature="stuck-retry")
    assert entry is not None and entry.issue_number == 81


def test_unresolved_comment_write_cannot_be_reissued_after_lease_expiry() -> None:
    """A second client entering inside add_comment cannot duplicate evidence."""
    client = FakeGitHubRefClient()
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    first_registry = GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id="engine-a",
        lease_seconds=30,
        clock=lambda: now[0],
    )
    second_registry = GitHubRefPatternRegistry(
        cast(Any, client),
        claimant_id="engine-b",
        lease_seconds=30,
        clock=lambda: now[0],
    )
    created = first_registry.reserve(_pending("run:a:A1"))
    first_registry.finalize(
        signature="stuck-retry",
        reservation_id=created.entry.reservation_id,
        issue_number=81,
    )
    observation = PatternObservation(
        observation_id="run:a:A2",
        comment="observed again\n\n<!-- obs:a2 -->",
    )
    comments: list[str] = []
    receipt = object()
    repository = MagicMock()
    repository.find_issue_comment_receipt.side_effect = lambda _number, *, body: (
        receipt if body in comments else None
    )
    second_owner = PatternCaseFileOwner(
        registry=second_registry,
        repository_host=repository,
        add_comment=repository.add_comment,
        before_write=lambda: None,
    )

    def delayed_comment(_number: int, body: str) -> str:
        now[0] += timedelta(seconds=31)
        with pytest.raises(
            AmbiguousPatternPublicationError, match="prevent a duplicate comment"
        ):
            second_owner.append_observations(
                signature="stuck-retry",
                issue_number=81,
                observations=(observation,),
                fix_class="code",
                area="runtime",
                diagnosis="",
            )
        comments.append(body)
        return "comment"

    first_owner = PatternCaseFileOwner(
        registry=first_registry,
        repository_host=repository,
        add_comment=delayed_comment,
        before_write=lambda: None,
    )
    outcome = first_owner.append_observations(
        signature="stuck-retry",
        issue_number=81,
        observations=(observation,),
        fix_class="code",
        area="runtime",
        diagnosis="",
    )

    assert outcome.recorded == 1
    assert comments == [observation.comment]
    entry = first_registry.read(signature="stuck-retry")
    assert entry is not None
    assert entry.observation_ids == ("run:a:A1", "run:a:A2")


def test_production_composition_initializes_shared_registry(tmp_path) -> None:
    client = FakeGitHubRefClient()
    host = GitHubAdapter(repo="owner/repo", http_client=cast(Any, client))
    config = Config(repo_root=tmp_path)
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_enabled = True
    config.tech_lead.authority.flag_pattern = "execute"

    registry = create_pattern_registry(config, host, InMemoryTechLeadAuthorityStore())

    assert isinstance(registry, MirroredPatternCaseFileRegistry)
    assert "refs/issue-orchestrator/registry/tech-lead-patterns" in client.refs


def test_promotion_settlement_initializes_shared_registry(tmp_path) -> None:
    """A promotion-only lane must retain retirement authority on restart."""
    client = FakeGitHubRefClient()
    host = GitHubAdapter(repo="owner/repo", http_client=cast(Any, client))
    config = Config(repo_root=tmp_path)
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_enabled = True
    config.tech_lead.authority.flag_pattern = "propose"
    config.tech_lead.findings.promote = "auto"

    registry = create_pattern_registry(
        config, host, InMemoryTechLeadAuthorityStore()
    )

    assert isinstance(registry, MirroredPatternCaseFileRegistry)
    assert "refs/issue-orchestrator/registry/tech-lead-patterns" in client.refs
