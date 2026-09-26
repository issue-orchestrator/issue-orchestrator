"""The one owner of releasing a published PR's review (#7293)."""

from __future__ import annotations

from issue_orchestrator.control.action_results import ActionResult
from issue_orchestrator.control.actions import (
    AddLabelAction,
    ReleasePublishedReviewAction,
    RemoveLabelAction,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.reconciliation import ExternalSnapshot, ReconciliationRequired
from issue_orchestrator.control.published_review_release import (
    PublishedReviewRelease,
    ReviewReleaseStatus,
    apply_release_published_review,
)
from issue_orchestrator.domain.validated_work import ValidatedWorkState
from issue_orchestrator.infra.config import Config

from tests.unit.control.published_review_support import (
    DispositionStore,
    PullRequests,
    custody,
    disposition,
    pr,
)

ISSUE = 382
LM = LabelManager(Config())


class _Labels:
    """Live issue labels plus the guarded writes the owner delegates to."""

    def __init__(self, labels, *, refuse_add=False, refuse_remove=False, board_moved=False):
        self.board_moved = board_moved
        self.live = set(labels)
        self.refuse_add = refuse_add
        self.refuse_remove = refuse_remove
        self.writes: list[tuple[str, str]] = []

    def read(self, issue_number):
        assert issue_number == ISSUE
        return sorted(self.live)

    def apply(self, action):
        if isinstance(action, AddLabelAction):
            assert action.fresh_presence, "the gate must not be a cached no-op"
            if self.refuse_add:
                return ActionResult.fail(action, "github refused the add")
            self.writes.append(("add", action.label))
            self.live.add(action.label)
            return ActionResult.ok(action)
        assert isinstance(action, RemoveLabelAction)
        assert action.expected is not None
        assert LM.blocked_failed in action.expected.required_labels
        assert LM.pr_pending in action.expected.required_labels
        if self.board_moved:
            raise ReconciliationRequired(
                entity_type="issue", entity_id=ISSUE,
                expected=ExternalSnapshot.for_issue(ISSUE, {LM.blocked_failed, LM.pr_pending}),
                actual=ExternalSnapshot.for_issue(ISSUE, {LM.blocked_failed}),
                reason="pr-pending was removed on GitHub",
            )
        assert LM.needs_human in action.expected.forbidden_labels
        if self.refuse_remove:
            return ActionResult.fail(action, "github refused the remove")
        self.writes.append(("remove", action.label))
        self.live.discard(action.label)
        return ActionResult.ok(action)


def _owner(labels: _Labels, pr_state: str = "open") -> PublishedReviewRelease:
    store = DispositionStore(
        {ISSUE: (disposition(ISSUE, ValidatedWorkState.RECOVERED, pr_number=500),)}
    )
    return PublishedReviewRelease(
        custody=custody(store, PullRequests({ISSUE: [pr(ISSUE, 500, state=pr_state)]})),
        labels=LM,
        read_labels=labels.read,
        apply=labels.apply,
    )


def test_the_gate_goes_on_before_the_block_comes_off():
    labels = _Labels(["agent:web", LM.blocked_failed])

    outcome = _owner(labels).release(ISSUE)

    assert outcome.status is ReviewReleaseStatus.RELEASED
    assert labels.writes == [("add", LM.pr_pending), ("remove", LM.blocked_failed)]
    assert labels.live == {"agent:web", LM.pr_pending}


def test_a_failed_gate_leaves_the_block_in_place():
    labels = _Labels(["agent:web", LM.blocked_failed], refuse_add=True)

    outcome = _owner(labels).release(ISSUE)

    assert outcome.status is ReviewReleaseStatus.GATE_FAILED
    assert labels.writes == []
    assert LM.blocked_failed in labels.live


def test_a_pr_closed_since_the_sweep_saw_it_releases_nothing():
    """Custody is rechecked at apply time: closing the PR is abandonment."""
    labels = _Labels(["agent:web", LM.blocked_failed])

    outcome = _owner(labels, pr_state="closed").release(ISSUE)

    assert outcome.status is ReviewReleaseStatus.NOT_HELD
    assert labels.writes == []


def test_a_block_with_its_own_owner_is_never_lifted():
    labels = _Labels(["agent:web", LM.blocked_failed, LM.blocked])

    outcome = _owner(labels).release(ISSUE)

    assert outcome.status is ReviewReleaseStatus.NOT_RELEASABLE
    assert labels.writes == []


def test_a_failed_block_removal_is_reported_with_the_gate_kept():
    labels = _Labels(["agent:web", LM.blocked_failed], refuse_remove=True)

    outcome = _owner(labels).release(ISSUE)

    assert outcome.status is ReviewReleaseStatus.BLOCK_REMOVAL_FAILED
    assert LM.pr_pending in labels.live


def test_the_applier_maps_each_outcome(monkeypatch):
    from issue_orchestrator.control import published_review_release as module

    action = ReleasePublishedReviewAction(issue_number=ISSUE)
    for labels, expected in (
        (_Labels(["agent:web", LM.blocked_failed]), "success"),
        (_Labels(["agent:web", LM.blocked_failed, LM.blocked]), "skipped"),
        (_Labels(["agent:web", LM.blocked_failed], refuse_add=True), "failure"),
    ):
        owner = _owner(labels)
        monkeypatch.setattr(module, "published_review_release_for", lambda _applier, o=owner: o)
        assert apply_release_published_review(action, object()).result_type.value == expected


def test_a_moved_board_keeps_the_block_and_reports_no_release():
    """pr-pending vanished (or needs-human landed) between the add and the lift."""
    labels = _Labels(["agent:web", LM.blocked_failed], board_moved=True)

    outcome = _owner(labels).release(ISSUE)

    assert outcome.status is ReviewReleaseStatus.BLOCK_REMOVAL_FAILED
    assert LM.blocked_failed in labels.live
