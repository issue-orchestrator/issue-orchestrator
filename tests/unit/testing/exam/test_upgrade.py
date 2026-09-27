"""Case U (#7432): grading an upgrade over the old engine's in-flight state."""

from __future__ import annotations

from dataclasses import replace

import pytest

from issue_orchestrator.testing.exam import ExamObservation, grade, render_summary
from issue_orchestrator.testing.exam.cases import (
    CODING,
    REVIEW,
    UPGRADE_EARLY_TICKS,
    UPGRADE_WITH_WORK_IN_FLIGHT,
    upgrade_with_work_in_flight,
)
from issue_orchestrator.testing.exam.observation import PullRequestState
from issue_orchestrator.testing.exam.upgrade import (
    LabelChange,
    UpgradeFacts,
    UpgradeSpec,
    WriteKind,
    classify_write,
    complete_history,
    grade_upgrade,
    hazard_events,
    label_changes,
    merged_by_id,
    writes_by_kind,
)
from tests.unit.testing.exam.builders import item, observation, pr

HOLD = frozenset({"io:needs-human", "blocked-failed", "needs-reconcile"})
CASE_U = upgrade_with_work_in_flight(code_reviewed_label="code-reviewed", hold_labels=HOLD)
SPEC = UpgradeSpec(early_ticks=UPGRADE_EARLY_TICKS, hold_labels=HOLD)


def facts(**overrides: object) -> UpgradeFacts:
    base = UpgradeFacts(
        base_commit="72d207e" + "0" * 33,
        candidate_commit="a120d59" + "0" * 33,
        sessions_at_stop=(911, 921),
        early_ticks=UPGRADE_EARLY_TICKS,
        early_writes={kind: 0 for kind in WriteKind},
        early_label_changes=(),
        hazards=(),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def finished(role: str, issue: int, pr_number: int):
    done = pr(state=PullRequestState.READY, labels=("code-reviewed",), number=pr_number)
    return replace(item(prs=(done,)), role=role, issue_number=issue)


def upgrade_observation(upgrade: UpgradeFacts | None) -> ExamObservation:
    coding = finished(CODING, 911, 912)
    review = finished(REVIEW, 921, 922)
    single = observation(UPGRADE_WITH_WORK_IN_FLIGHT, coding)
    return replace(
        single,
        items=(coding, review),
        owned_numbers=frozenset({911, 912, 921, 922}),
        upgrade=upgrade,
    )


class TestWriteClassification:
    @pytest.mark.parametrize(
        ("command", "kind"),
        [
            ("GET /repos/o/r/issues/5/labels", None),
            ("GET /search/issues", None),
            ("POST /repos/o/r/issues/5/labels", WriteKind.LABEL_ADD),
            ("PUT /repos/o/r/issues/5/labels", WriteKind.LABEL_ADD),
            ("DELETE /repos/o/r/issues/5/labels/needs-human", WriteKind.LABEL_REMOVE),
            ("POST /repos/o/r/issues/5/comments", WriteKind.COMMENT),
            ("PATCH /repos/o/r/issues/5", WriteKind.ISSUE_EDIT),
            ("POST /repos/o/r/pulls", WriteKind.PULL_REQUEST),
            ("PUT /repos/o/r/pulls/7/merge", WriteKind.PULL_REQUEST),
            ("POST /graphql", WriteKind.GRAPHQL),
            ("POST /repos/o/r/git/refs", WriteKind.OTHER),
        ],
    )
    def test_each_audit_key_is_one_kind(self, command: str, kind: WriteKind | None) -> None:
        assert classify_write(command) is kind

    def test_writes_are_counted_by_kind_and_reads_skipped(self) -> None:
        counts = writes_by_kind(
            {
                "GET /repos/o/r/issues/5": 40,
                "POST /repos/o/r/issues/5/comments": 2,
                "POST /repos/o/r/issues/6/comments": 1,
                "DELETE /repos/o/r/issues/5/labels/x": 1,
            }
        )
        assert counts[WriteKind.COMMENT] == 3
        assert counts[WriteKind.LABEL_REMOVE] == 1
        assert sum(counts.values()) == 4


class TestEventReaders:
    def test_hazards_are_the_restore_owners_escalations(self) -> None:
        events = [
            {"type": "tick.completed", "payload": {}},
            {"type": "session.run_unrestorable", "payload": {"issue_number": 921, "cause": "RUN_UNRESTORABLE"}},
            {"type": "session.claim_unreadable", "payload": {"issue_number": 911, "error": "bad json"}},
            {"type": "stale.in_progress_detected", "payload": {"issue_number": 911}},
        ]
        assert hazard_events(events) == (
            "session.run_unrestorable on #921 (RUN_UNRESTORABLE)",
            "session.claim_unreadable on #911 (bad json)",
        )

    def test_label_changes_keep_both_directions(self) -> None:
        events = [
            {"type": "issue.labels_changed", "payload": {"issue_number": 911, "added": ["in-progress"], "removed": []}},
            {"type": "pr.view_changed", "payload": {"issue_number": 911}},
        ]
        assert label_changes(events) == (LabelChange(911, ("in-progress",), ()),)


class TestUpgradeGrade:
    def test_a_quiet_takeover_passes(self) -> None:
        benign = (LabelChange(911, ("in-progress",), ("stale",)),)
        writes = {**{kind: 0 for kind in WriteKind}, WriteKind.LABEL_ADD: 1, WriteKind.LABEL_REMOVE: 1}
        assert grade_upgrade(SPEC, facts(early_label_changes=benign, early_writes=writes)).passed

    def test_a_restore_hazard_fails(self) -> None:
        grade_ = grade_upgrade(SPEC, facts(hazards=("session.run_unrestorable on #921 (x)",)))
        assert grade_.failures == ("restore hazard: session.run_unrestorable on #921 (x)",)

    def test_a_comment_in_the_window_fails(self) -> None:
        writes = {**{kind: 0 for kind in WriteKind}, WriteKind.COMMENT: 2}
        assert grade_upgrade(SPEC, facts(early_writes=writes)).failures == (
            "2 comment(s) posted in the restart window",
        )

    def test_a_hold_label_in_the_window_fails(self) -> None:
        paged = (LabelChange(921, ("io:needs-human",), ()),)
        assert grade_upgrade(SPEC, facts(early_label_changes=paged)).failures == (
            "hold label added in the restart window: #921 +io:needs-human",
        )

    def test_a_hold_label_REMOVED_is_not_a_page(self) -> None:
        cleared = (LabelChange(921, (), ("io:needs-human",)),)
        assert grade_upgrade(SPEC, facts(early_label_changes=cleared)).passed

    def test_an_engine_that_stops_ticking_fails(self) -> None:
        assert grade_upgrade(SPEC, facts(early_ticks=2)).failures == (
            f"candidate completed only 2 of {UPGRADE_EARLY_TICKS} ticks before the held work was released",
        )

    def test_a_spec_needs_hold_labels_and_a_window(self) -> None:
        with pytest.raises(ValueError):
            UpgradeSpec(early_ticks=0, hold_labels=HOLD)
        with pytest.raises(ValueError):
            UpgradeSpec(early_ticks=5, hold_labels=frozenset())


class TestCaseU:
    def test_both_items_finished_and_a_quiet_takeover_pass(self) -> None:
        card = grade(CASE_U, upgrade_observation(facts()))
        assert card.passed, card.failures

    def test_a_quarantine_fails_the_case_even_with_the_work_finished(self) -> None:
        card = grade(CASE_U, upgrade_observation(facts(hazards=("session.run_unrestorable on #921 (x)",))))
        assert all(goal.passed for goal in card.goals)
        assert card.failures == ("upgrade: restore hazard: session.run_unrestorable on #921 (x)",)
        summary = render_summary(card)
        assert "upgrade [FAIL]: 72d207e000 -> a120d59000" in summary
        assert "restore hazards: session.run_unrestorable on #921 (x)" in summary

    def test_unfinished_review_work_fails_its_goals(self) -> None:
        obs = upgrade_observation(facts())
        stuck = replace(obs.items[1], pull_requests=(pr(state=PullRequestState.DRAFT, number=922),))
        card = grade(CASE_U, replace(obs, items=(obs.items[0], stuck)))
        assert "goal review.pr_merged/ready: PR #922 is draft" in card.failures

    def test_a_hold_label_left_on_an_item_fails_its_goals(self) -> None:
        obs = upgrade_observation(facts())
        held = replace(obs.items[0], issue_labels=frozenset({"blocked-failed"}))
        card = grade(CASE_U, replace(obs, items=(held, obs.items[1])))
        assert card.failures == ("goal coding.issue_free_of_blocks: issue #911 carries ['blocked-failed']",)

    def test_an_upgrade_case_needs_upgrade_facts(self) -> None:
        with pytest.raises(ValueError, match="observed without upgrade facts"):
            grade(CASE_U, upgrade_observation(None))

    def test_upgrade_facts_round_trip_through_the_saved_observation(self) -> None:
        paged = (LabelChange(921, ("io:needs-human",), ()),)
        writes = {**{kind: 0 for kind in WriteKind}, WriteKind.COMMENT: 1}
        obs = upgrade_observation(facts(early_label_changes=paged, early_writes=writes))
        again = ExamObservation.from_dict(obs.to_dict())
        assert again == obs
        assert grade(CASE_U, again).failures == grade(CASE_U, obs).failures


class TestEventHistory:
    def test_a_complete_history_is_accepted(self) -> None:
        events = [{"event_id": i, "type": "tick.completed"} for i in (1, 2, 3)]
        assert complete_history(events) == events

    @pytest.mark.parametrize(
        "ids",
        [[], [4, 5, 6], [1, 2, 4]],
        ids=["empty", "buffer dropped its oldest", "hole"],
    )
    def test_a_truncated_history_cannot_be_graded(self, ids: list[int]) -> None:
        with pytest.raises(ValueError, match="incomplete"):
            complete_history([{"event_id": i} for i in ids])

    def test_startup_hazards_seen_only_in_the_replay_are_kept(self) -> None:
        replayed = [
            {"event_id": 1, "type": "session.run_unrestorable", "payload": {"issue_number": 921, "cause": "X"}},
            {"event_id": 2, "type": "tick.completed", "payload": {}},
        ]
        live = [
            {"event_id": 2, "type": "tick.completed", "payload": {}},
            {"event_id": 3, "type": "session.claim_unreadable", "payload": {"issue_number": 911, "cause": "Y"}},
        ]
        merged = merged_by_id(live, replayed)  # live first: order comes from ids
        assert [event["event_id"] for event in merged] == [1, 2, 3]
        assert hazard_events(merged) == (
            "session.run_unrestorable on #921 (X)",
            "session.claim_unreadable on #911 (Y)",
        )
