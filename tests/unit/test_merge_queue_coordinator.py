"""Tests for the merge queue coordinator — the single owner of merge-queue policy."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.merge_queue_coordinator import (
    MergeQueueCoordinator,
    decide_merge_queue_action,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.events import EventContext, EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models import MergeQueueConfig
from issue_orchestrator.ports import InMemoryEventSink
from issue_orchestrator.ports.pull_request_tracker import MergeQueueEntry, PRInfo
from issue_orchestrator.ports.repository_host import RepositoryHostError


def _pr(
    mergeable_state: str | None,
    *,
    status_check_rollup: str | None = None,
    labels: list[str] | None = None,
) -> PRInfo:
    return PRInfo(
        number=318,
        title="Add coalescing",
        url="https://github.com/owner/repo/pull/318",
        branch="228-cache",
        body="",
        state="open",
        labels=labels if labels is not None else ["code-reviewed"],
        mergeable_state=mergeable_state,
        status_check_rollup=status_check_rollup,  # type: ignore[arg-type]
    )


def _issue() -> Issue:
    return Issue(
        number=228,
        title="Shared cache read misses",
        labels=["agent:backend", "pr-pending", "code-reviewed"],
        state="open",
    )


def _coordinator(
    repository_host: MagicMock,
    *,
    failure_action: str = "rework",
    enabled: bool = True,
) -> tuple[MergeQueueCoordinator, InMemoryEventSink]:
    events = InMemoryEventSink()
    coordinator = MergeQueueCoordinator(
        config=MergeQueueConfig(enabled=enabled, failure_action=failure_action),
        repository_host=repository_host,
        events=events,
        event_context=EventContext(),
        label_manager=LabelManager(Config()),
    )
    return coordinator, events


# --------------------------------------------------------------------------- #
# Pure decision matrix
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "mergeable_state,rollup,expected",
    [
        ("clean", None, "ENQUEUE"),
        ("behind", None, "ENQUEUE"),          # behind-base is enqueue-eligible, NOT rework
        ("blocked", "SUCCESS", "ENQUEUE"),    # merge-queue-required state
        ("dirty", None, "REWORK_CONFLICT"),
        ("unstable", "FAILURE", "REWORK_CHECK_FAILED"),
        ("blocked", "ERROR", "REWORK_CHECK_FAILED"),
        ("unstable", "PENDING", "WAIT"),      # checks running → observe
        ("unknown", None, "WAIT"),            # mergeability unknown → wait/retry
    ],
)
def test_decide_for_unqueued_pr(mergeable_state, rollup, expected) -> None:
    assert (
        decide_merge_queue_action(_pr(mergeable_state, status_check_rollup=rollup), None)
        == expected
    )


@pytest.mark.parametrize(
    "state,expected",
    [
        ("QUEUED", "WAIT"),
        ("AWAITING_CHECKS", "WAIT"),
        ("MERGEABLE", "WAIT"),
        ("PENDING", "WAIT"),
        ("LOCKED", "WAIT"),
        ("UNMERGEABLE", "ROUTE_FAILURE"),
    ],
)
def test_decide_for_queued_pr_ignores_mergeable_state(state, expected) -> None:
    # Even a "dirty" PR that is already in the queue is observed (or routed on
    # failure) by its queue entry, never re-classified by mergeable_state.
    assert decide_merge_queue_action(_pr("dirty"), MergeQueueEntry(state)) == expected


# --------------------------------------------------------------------------- #
# classify() — fact production + side effects
# --------------------------------------------------------------------------- #


def test_clean_pr_produces_enqueue_fact() -> None:
    repo = MagicMock()
    repo.read_merge_queue_entry.return_value = None
    coordinator, _ = _coordinator(repo)

    followup = coordinator.classify(
        pr=_pr("clean"), issue=_issue(), issue_number=228, pr_number=318, entry=None
    )

    assert followup.enqueue is not None
    assert followup.enqueue.pr_number == 318
    assert followup.enqueue.issue_number == 228
    assert followup.rework is None and followup.escalation is None
    # Enqueue mutation is NOT performed here — that is the applier's job.
    repo.enqueue_to_merge_queue.assert_not_called()


def test_pr_without_gate_label_is_not_enqueued() -> None:
    """A PR that has not cleared the enqueue_after gate is never enqueued."""
    repo = MagicMock()
    coordinator, _ = _coordinator(repo)

    followup = coordinator.classify(
        pr=_pr("clean", labels=[]),  # missing code-reviewed
        issue=_issue(),
        issue_number=228,
        pr_number=318,
        entry=None,
    )

    assert followup.enqueue is None
    assert followup == type(followup)()


def test_behind_base_pr_is_enqueued_not_reworked() -> None:
    repo = MagicMock()
    coordinator, _ = _coordinator(repo)

    followup = coordinator.classify(
        pr=_pr("behind"), issue=_issue(), issue_number=228, pr_number=318, entry=None
    )

    assert followup.enqueue is not None
    assert followup.rework is None


def test_conflict_routes_to_rework() -> None:
    repo = MagicMock()
    repo.issue_comment_marker_present.return_value = False
    coordinator, _ = _coordinator(repo)

    followup = coordinator.classify(
        pr=_pr("dirty"), issue=_issue(), issue_number=228, pr_number=318, entry=None
    )

    assert followup.rework is not None
    assert followup.enqueue is None
    assert "Merge conflict" in (followup.rework.feedback or "")


def test_queued_pr_waits() -> None:
    repo = MagicMock()
    coordinator, events = _coordinator(repo)

    followup = coordinator.classify(
        pr=_pr("blocked"),
        issue=_issue(),
        issue_number=228,
        pr_number=318,
        entry=MergeQueueEntry("QUEUED", position=2),
    )

    assert followup == type(followup)()  # all None — observe next tick
    assert events.events == []


def test_queue_failure_routes_to_rework_and_emits_event() -> None:
    repo = MagicMock()
    repo.issue_comment_marker_present.return_value = False
    coordinator, events = _coordinator(repo, failure_action="rework")

    followup = coordinator.classify(
        pr=_pr("blocked"),
        issue=_issue(),
        issue_number=228,
        pr_number=318,
        entry=MergeQueueEntry("UNMERGEABLE"),
    )

    assert followup.rework is not None
    assert followup.escalation is None
    assert "merge queue" in (followup.rework.feedback or "").lower()
    failed = events.get_events(EventName.MERGE_QUEUE_FAILED.value)
    assert len(failed) == 1
    assert failed[0].data["failure_action"] == "rework"


def test_queue_failure_routes_to_needs_human_when_configured() -> None:
    repo = MagicMock()
    coordinator, events = _coordinator(repo, failure_action="needs_human")

    followup = coordinator.classify(
        pr=_pr("blocked"),
        issue=_issue(),
        issue_number=228,
        pr_number=318,
        entry=MergeQueueEntry("UNMERGEABLE"),
    )

    assert followup.escalation is not None
    assert followup.escalation.kind == "merge_queue_failed"
    assert followup.rework is None
    assert len(events.get_events(EventName.MERGE_QUEUE_FAILED.value)) == 1


def test_read_entry_swallows_transient_error() -> None:
    repo = MagicMock()
    repo.read_merge_queue_entry.side_effect = RepositoryHostError("boom")
    coordinator, _ = _coordinator(repo)

    assert coordinator.read_entry(318) is None


def test_disabled_coordinator_reports_disabled() -> None:
    repo = MagicMock()
    coordinator, _ = _coordinator(repo, enabled=False)
    assert coordinator.enabled is False
