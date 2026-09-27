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


def test_a_park_labels_comments_and_publishes(mock_event_sink) -> None:
    applier = _Applier()

    committed = _escalation(mock_event_sink, applier).escalate(_row())

    assert committed is True
    label, comment = applier.applied
    assert isinstance(label, AddLabelAction)
    assert (label.issue_number, label.label) == (410, "needs-human")
    assert label.needs_human_cause is NeedsHumanCause.ACTION_LIVENESS
    assert isinstance(comment, AddCommentAction) and comment.number == 410
    assert "`remove_label` on `issue:410`" in comment.comment
    [event] = mock_event_sink.get_events_by_name(EventName.ACTION_PARKED)
    assert event.data["issue_number"] == 410
    assert event.data["outcome"] == "needs_human"


def test_an_uncommitted_block_is_reported_and_posts_no_comment(mock_event_sink) -> None:
    applier = _Applier(fail=frozenset({AddLabelAction}))

    assert _escalation(mock_event_sink, applier).escalate(_row()) is False
    assert [type(action) for action in applier.applied] == [AddLabelAction]
    assert mock_event_sink.get_events_by_name(EventName.ACTION_PARKED)


def test_a_park_with_no_issue_is_still_on_the_timeline(mock_event_sink) -> None:
    applier = _Applier()

    assert _escalation(mock_event_sink, applier).escalate(_row(issue=None)) is False
    assert applier.applied == []
    assert mock_event_sink.get_events_by_name(EventName.ACTION_PARKED)


def test_resolve_releases_only_this_cause_and_only_when_asked(mock_event_sink) -> None:
    applier = _Applier()
    escalation = _escalation(mock_event_sink, applier)

    escalation.resolve((_row(escalated=True),), release_issue=False)
    assert applier.applied == []

    escalation.resolve((_row(escalated=True), _row(escalated=False)), release_issue=True)
    [release] = applier.applied
    assert isinstance(release, RemoveLabelAction)
    assert release.needs_human_cause is NeedsHumanCause.ACTION_LIVENESS
    assert len(mock_event_sink.get_events_by_name(EventName.ACTION_RELEASED)) == 3
