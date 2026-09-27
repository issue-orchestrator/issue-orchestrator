"""How a parked action reaches a person, at the applier and event ports (#7350)."""

from __future__ import annotations

from datetime import datetime, timezone

from issue_orchestrator.control.action_liveness_escalation import ActionLivenessEscalation
from issue_orchestrator.control.actions import (
    ActionResult,
    AddCommentAction,
    AddLabelAction,
    RemoveLabelAction,
)
from issue_orchestrator.domain.action_liveness import (
    ActionIdentity,
    LivenessKey,
    LivenessRow,
    OutcomeKind,
)
from issue_orchestrator.domain.human_block import NeedsHumanCause
from issue_orchestrator.events import EventName

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _row(issue: int | None = 410, escalated: bool = False) -> LivenessRow:
    return LivenessRow(
        key=LivenessKey(ActionIdentity("issue:410", "remove_label"), "a" * 32, issue),
        attempts=1,
        first_failed_at=NOW,
        last_failed_at=NOW,
        last_outcome=OutcomeKind.NEEDS_HUMAN,
        last_reason="subject is paused",
        next_attempt_at=None,
        escalated=escalated,
    )


class _Applier:
    def __init__(self, fail: frozenset[type] = frozenset()) -> None:
        self.applied: list = []
        self.fail = fail

    def apply(self, action):
        self.applied.append(action)
        if type(action) in self.fail:
            return ActionResult.fail(action, "403")
        return ActionResult.ok(action)


def _escalation(mock_event_sink, applier):
    return ActionLivenessEscalation(
        events=mock_event_sink, applier=applier, needs_human_label="needs-human"
    )


def test_a_block_labels_under_its_own_cause_and_comments(mock_event_sink) -> None:
    applier = _Applier()

    escalation = _escalation(mock_event_sink, applier)
    assert escalation.block(_row()) is True
    assert escalation.explain(_row()) is True

    label, comment = applier.applied
    assert isinstance(label, AddLabelAction)
    assert (label.issue_number, label.label) == (410, "needs-human")
    assert label.needs_human_cause is NeedsHumanCause.ACTION_LIVENESS
    assert isinstance(comment, AddCommentAction) and comment.number == 410
    assert "`remove_label` on `issue:410`" in comment.comment


def test_uncommitted_writes_are_reported(mock_event_sink) -> None:
    applier = _Applier(fail=frozenset({AddLabelAction, AddCommentAction}))
    escalation = _escalation(mock_event_sink, applier)

    assert escalation.block(_row()) is False
    assert escalation.explain(_row()) is False


def test_a_raising_write_is_an_uncommitted_one(mock_event_sink) -> None:
    class _Raising:
        def apply(self, action):
            raise RuntimeError("claim lost")

    assert _escalation(mock_event_sink, _Raising()).block(_row()) is False
    assert _escalation(mock_event_sink, _Raising()).unblock(410) is False


def test_announcements_reach_the_timeline(mock_event_sink) -> None:
    escalation = _escalation(mock_event_sink, _Applier())

    escalation.announce_parked(_row())
    escalation.announce_released((_row(), _row(issue=None)))

    [parked] = mock_event_sink.get_events_by_name(EventName.ACTION_PARKED)
    assert parked.data["issue_number"] == 410
    assert parked.data["outcome"] == "needs_human"
    released = mock_event_sink.get_events_by_name(EventName.ACTION_RELEASED)
    assert len(released) == 2 and "issue_number" not in released[1].data


def test_unblock_withdraws_only_this_cause(mock_event_sink) -> None:
    applier = _Applier()

    assert _escalation(mock_event_sink, applier).unblock(410) is True
    [release] = applier.applied
    assert isinstance(release, RemoveLabelAction)
    assert release.issue_number == 410
    assert release.needs_human_cause is NeedsHumanCause.ACTION_LIVENESS


# --- The shared block owner, for a replayed liveness release (review r6) ----


class _Labels:
    def __init__(self) -> None:
        self.live: dict[int, set[str]] = {}

    def add_label(self, issue_number: int, label: str) -> None:
        self.live.setdefault(issue_number, set()).add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.live.setdefault(issue_number, set()).discard(label)

    def read(self, issue_number: int):
        return tuple(self.live.get(issue_number, ()))


def _shared_block(tmp_path):
    from issue_orchestrator.control.needs_human_block import NeedsHumanBlock
    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )

    labels = _Labels()
    block = NeedsHumanBlock(
        "needs-human", "tech-lead-needs-human", labels, labels.read, frozenset,
        SqlitePendingWorkClaimStore(tmp_path / "causes.sqlite"),
    )
    return labels, block


def test_a_liveness_release_takes_off_the_block_it_recorded(tmp_path) -> None:
    from issue_orchestrator.domain.human_block import BlockOutcome, HumanBlockRequest

    labels, block = _shared_block(tmp_path)
    request = HumanBlockRequest(410, NeedsHumanCause.ACTION_LIVENESS, "parked")
    assert block.acquire(request) is BlockOutcome.HELD

    assert block.release(request) is BlockOutcome.CLEARED
    assert "needs-human" not in labels.live[410]


def test_a_replayed_liveness_release_leaves_a_persons_new_block_alone(tmp_path) -> None:
    """The owed release outlived a force-clear, and a person put the label
    back. The replay must not take it off."""
    from issue_orchestrator.domain.human_block import BlockOutcome, HumanBlockRequest

    labels, block = _shared_block(tmp_path)
    request = HumanBlockRequest(410, NeedsHumanCause.ACTION_LIVENESS, "parked")
    block.acquire(request)
    assert block.force_clear(410, "terminal recovery").committed
    labels.add_label(410, "needs-human")  # a person, with no recorded cause

    assert block.release(request) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert "needs-human" in labels.live[410]
