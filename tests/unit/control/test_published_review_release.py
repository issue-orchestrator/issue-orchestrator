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
PR = 500
REVIEW_LABEL = "needs-code-review"
LM = LabelManager(Config())


class _Labels:
    """Live issue labels plus the guarded writes the owner delegates to."""

    def __init__(self, labels, *, refuse_add=False, refuse_remove=False, board_moved=False,
                 refuse_route=False):
        self.board_moved = board_moved
        self.refuse_route = refuse_route
        self.pr_live: set[str] = set()
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
            if action.issue_number == PR:
                if self.refuse_route:
                    return ActionResult.fail(action, "github refused the PR label")
                self.writes.append(("add-pr", action.label))
                self.pr_live.add(action.label)
                return ActionResult.ok(action)
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


def _owner(labels: _Labels, pr_state: str = "open", pr_labels=()) -> PublishedReviewRelease:
    store = DispositionStore(
        {ISSUE: (disposition(ISSUE, ValidatedWorkState.RECOVERED, pr_number=500),)}
    )
    return PublishedReviewRelease(
        custody=custody(store, PullRequests({ISSUE: [pr(ISSUE, 500, state=pr_state, labels=pr_labels)]})),
        labels=LM,
        read_labels=labels.read,
        apply=labels.apply,
        review_label=REVIEW_LABEL,
    )


def test_the_gate_goes_on_before_the_block_comes_off():
    labels = _Labels(["agent:web", LM.blocked_failed])

    outcome = _owner(labels).release(ISSUE)

    assert outcome.status is ReviewReleaseStatus.RELEASED
    assert labels.writes == [
        ("add", LM.pr_pending), ("add-pr", REVIEW_LABEL), ("remove", LM.blocked_failed)
    ]
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
        monkeypatch.setattr(module, "published_review_release_for", lambda _applier, _label, o=owner: o)
        assert apply_release_published_review(action, object()).result_type.value == expected


def test_a_moved_board_keeps_the_block_and_reports_no_release():
    """pr-pending vanished (or needs-human landed) between the add and the lift."""
    labels = _Labels(["agent:web", LM.blocked_failed], board_moved=True)

    outcome = _owner(labels).release(ISSUE)

    assert outcome.status is ReviewReleaseStatus.BLOCK_REMOVAL_FAILED
    assert LM.blocked_failed in labels.live


def test_a_pr_with_its_own_block_is_not_released():
    """A terminated review blocks the PR too; lifting only the issue block
    would report a release while review discovery still rejects the PR."""
    labels = _Labels(["agent:web", LM.blocked_failed])

    outcome = _owner(labels, pr_labels=(LM.blocked_failed,)).release(ISSUE)

    assert outcome.status is ReviewReleaseStatus.NOT_RELEASABLE
    assert labels.writes == []
    assert LM.blocked_failed in labels.live


def test_a_pr_that_lost_its_review_label_is_routed_before_the_block_lifts():
    """#7293 round 9: review discovery scans the review label; without it a
    released issue has no route to review."""
    labels = _Labels(["agent:web", LM.blocked_failed])

    outcome = _owner(labels).release(ISSUE)

    assert outcome.released
    assert labels.pr_live == {REVIEW_LABEL}
    assert labels.writes.index(("add-pr", REVIEW_LABEL)) < labels.writes.index(("remove", LM.blocked_failed))


def test_an_unroutable_pr_keeps_the_block():
    labels = _Labels(["agent:web", LM.blocked_failed], refuse_route=True)

    outcome = _owner(labels).release(ISSUE)

    assert outcome.status is ReviewReleaseStatus.ROUTE_FAILED
    assert LM.blocked_failed in labels.live
    assert ("remove", LM.blocked_failed) not in labels.writes


def test_the_sweeps_release_command_runs_through_the_real_applier_dispatch():
    """#7293 round 10: the planner's command must reach the owner via ActionApplier."""
    from unittest.mock import MagicMock

    from issue_orchestrator.control.action_results import ActionResultType
    from issue_orchestrator.control.published_review_release import (
        build_stuck_sweep_review_release_actions,
    )
    from tests.runtime_lifecycle_helpers import make_action_applier, runtime_owners

    live = {ISSUE: {"agent:web", LM.blocked_failed}, PR: set()}

    class _GitHubLabels:
        def has_label(self, number, label):
            return label in live[number]

        def add_label(self, number, label):
            live[number].add(label)

        def remove_label(self, number, label):
            live[number].discard(label)

        def read_issue_labels(self, number):
            return sorted(live[number])

        def get_issue_labels_fresh(self, number):
            return sorted(live[number])

    github = _GitHubLabels()
    applier = make_action_applier(
        labels=github, sessions=MagicMock(), events=MagicMock(), repository_host=github,
        fresh_issue_reader=github, label_manager=LM, reconcile=False,
    )
    store = DispositionStore(
        {ISSUE: (disposition(ISSUE, ValidatedWorkState.RECOVERED, pr_number=PR),)}
    )
    applier.runtime_lifecycle = runtime_owners(
        published_review=custody(store, PullRequests({ISSUE: [pr(ISSUE, PR)]})))

    (action,) = build_stuck_sweep_review_release_actions((ISSUE,), REVIEW_LABEL)
    result = applier.apply(action)

    assert result.result_type is ActionResultType.SUCCESS, result.error
    assert live[ISSUE] == {"agent:web", LM.pr_pending}
    assert live[PR] == {REVIEW_LABEL}
