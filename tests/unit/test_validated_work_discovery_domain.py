"""Fail-closed invariants for cold retained-work discovery facts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from issue_orchestrator.domain.validated_work import (
    FinalizationPhase,
    LineageRole,
    ValidatedWorkKey,
    ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_commands import (
    AbandonStatus,
    ValidatedWorkDisposition,
)
from issue_orchestrator.domain.validated_work_discovery import (
    ValidatedWorkDiscovery,
    ValidatedWorkSnapshot,
    WorkDiscoveryStatus,
)


def _snapshot(
    *, state: ValidatedWorkState = ValidatedWorkState.PARKED
) -> ValidatedWorkSnapshot:
    key = ValidatedWorkKey("owner/repo", 7, "feature", "1" * 40)
    disposition = ValidatedWorkDisposition(
        key.record_id,
        key,
        "evidence-1",
        state,
        LineageRole.HEAD,
        "retained",
    )
    return ValidatedWorkSnapshot(
        disposition=disposition,
        record_id=disposition.record_id,
        validated_head_sha=key.validated_head_sha,
        worktree_head_sha=key.validated_head_sha,
        branch_name=key.branch_name,
        expected_remote_head_sha=None,
        superseded_evidence_ids=(),
        attached_evidence_ids=(),
        lineage_role=LineageRole.HEAD,
        escrow_retained=True,
        observation_revision=0,
        waits_on_record_id="",
        owner=None,
        publish_attempts=0,
        finalization_phase=FinalizationPhase.NOT_STARTED,
        updated_at="2026-01-01T00:00:00+00:00",
        can_recover=True,
        can_abandon=True,
        abandon_unavailable=None,
    )


def test_snapshot_rejects_disagreeing_identity_and_action_shapes() -> None:
    snapshot = _snapshot()

    with pytest.raises(ValueError, match="branches disagree"):
        replace(snapshot, branch_name="other")
    with pytest.raises(ValueError, match="unavailable abandonment"):
        replace(snapshot, can_abandon=False, abandon_unavailable=None)
    with pytest.raises(ValueError, match="abandon availability contradicts"):
        replace(
            snapshot,
            abandon_unavailable=AbandonStatus.REFUSED_STATE,
        )
    with pytest.raises(ValueError, match="recover availability contradicts"):
        replace(
            snapshot,
            escrow_retained=False,
            can_abandon=False,
            abandon_unavailable=AbandonStatus.REFUSED_STATE,
        )
    with pytest.raises(ValueError, match="attached-evidence refusal"):
        replace(
            snapshot,
            can_abandon=False,
            abandon_unavailable=AbandonStatus.ATTACHED_EVIDENCE_PENDING,
        )


def test_discovery_accepts_empty_success_and_rejects_ambiguous_unavailability() -> None:
    empty = ValidatedWorkDiscovery(
        WorkDiscoveryStatus.AVAILABLE, (), "supported database has no retained work"
    )
    assert empty.records == ()

    with pytest.raises(ValueError, match="unavailable discovery"):
        ValidatedWorkDiscovery(
            WorkDiscoveryStatus.UNREADABLE, (_snapshot(),), "database unavailable"
        )


def test_discovery_rejects_resolved_unowned_records() -> None:
    key = ValidatedWorkKey("owner/repo", 7, "feature", "1" * 40)
    resolved = replace(
        _snapshot(),
        disposition=ValidatedWorkDisposition(
            key.record_id,
            key,
            "evidence-1",
            ValidatedWorkState.RECOVERED,
            LineageRole.HEAD,
            "published",
            published_head_sha=key.validated_head_sha,
        ),
        can_recover=False,
        can_abandon=False,
        abandon_unavailable=AbandonStatus.ALREADY_RESOLVED,
    )

    with pytest.raises(ValueError, match="unresolved or retained-owner"):
        ValidatedWorkDiscovery(
            WorkDiscoveryStatus.AVAILABLE, (resolved,), "discovery completed"
        )
