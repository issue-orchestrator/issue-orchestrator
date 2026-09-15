"""Timeline actor classification (#6969).

The scenario under test is real. A tech-lead failure investigation of issue
#6410 ran under issue #6410's number but inside the scratch worktree
``issue-orchestrator-tech-lead-6410-df24fde45b3b``. Its ``review.approved`` and
its branch push were recorded on #6410's timeline and a later health review read
them as the implementation being approved and published. The run directories in
these tests are copied verbatim from ``timeline.sqlite``.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.tech_lead_scratch_identity import (
    new_scratch_token,
    scratch_branch_name,
    scratch_worktree_name,
)
from issue_orchestrator.domain.timeline_actor import (
    TIMELINE_ACTOR_FIELD,
    TimelineActor,
    classify_timeline_actor,
    parse_timeline_actor,
    read_timeline_actor,
)

# Verbatim from timeline.sqlite: the #6410 investigation's own review exchange.
INVESTIGATION_RUN_DIR = (
    "/Users/brucegordon/dev/issue-orchestrator-tech-lead-6410-df24fde45b3b"
    "/.issue-orchestrator/sessions/20260728-023837Z__review-exchange-6410"
    "-20260728T023837582946Z"
)
# Verbatim from timeline.sqlite: #6410's own coding session.
IMPLEMENTATION_RUN_DIR = (
    "/Users/brucegordon/dev/issue-orchestrator-6410"
    "/.issue-orchestrator/sessions/20260727-214653Z__coding-1"
)


class TestHistoricalRecords:
    """Records written before the discriminator existed still classify."""

    def test_investigation_run_dir_is_not_the_issues_own_work(self) -> None:
        actor = classify_timeline_actor({"run_dir": INVESTIGATION_RUN_DIR})

        assert actor is TimelineActor.TECH_LEAD_INVESTIGATION
        assert not actor.is_issue_evidence

    def test_implementation_run_dir_is_the_issues_own_work(self) -> None:
        actor = classify_timeline_actor({"run_dir": IMPLEMENTATION_RUN_DIR})

        assert actor is TimelineActor.ISSUE_SESSION
        assert actor.is_issue_evidence

    def test_worktree_path_classifies_when_run_dir_is_absent(self) -> None:
        assert (
            classify_timeline_actor(
                {
                    "worktree_path": "/Users/brucegordon/dev/"
                    "issue-orchestrator-tech-lead-6410-df24fde45b3b"
                }
            )
            is TimelineActor.TECH_LEAD_INVESTIGATION
        )

    def test_investigation_branch_classifies_when_no_path_is_present(self) -> None:
        assert (
            classify_timeline_actor(
                {"branch_name": "tech-lead-investigation-6410-df24fde45b3b"}
            )
            is TimelineActor.TECH_LEAD_INVESTIGATION
        )


class TestUnattributableRecords:
    """An unattributable record is never silently promoted to evidence."""

    def test_record_without_durable_identity_is_unknown(self) -> None:
        # agent.coding_completed carries neither run_dir nor run_id.
        actor = classify_timeline_actor(
            {"session_name": "issue-6410", "outcome": "completed"}
        )

        assert actor is TimelineActor.UNKNOWN
        assert not actor.is_issue_evidence

    def test_empty_run_dir_does_not_count_as_an_answer(self) -> None:
        assert classify_timeline_actor({"run_dir": ""}) is TimelineActor.UNKNOWN


class TestExplicitStamp:
    """A producer's own declaration wins over any derivation."""

    def test_stamp_is_authoritative(self) -> None:
        actor = classify_timeline_actor(
            {
                TIMELINE_ACTOR_FIELD: TimelineActor.TECH_LEAD_INVESTIGATION.value,
                "run_dir": IMPLEMENTATION_RUN_DIR,
            }
        )

        assert actor is TimelineActor.TECH_LEAD_INVESTIGATION

    def test_unknown_vocabulary_fails_loudly(self) -> None:
        # A value from a newer writer is a schema break, not an unattributable
        # record: collapsing it into UNKNOWN would hide the break.
        with pytest.raises(ValueError, match="Unknown timeline_actor value"):
            classify_timeline_actor({TIMELINE_ACTOR_FIELD: "reviewer-session"})

    def test_non_string_stamp_fails_loudly(self) -> None:
        with pytest.raises(TypeError):
            parse_timeline_actor(7)


class TestGeneratedIdentitiesAreRecognised:
    """The classifier must track the generator, not a stale copy of it."""

    @pytest.mark.parametrize("issue_number", [1, 42, 6410, 999999])
    def test_freshly_generated_scratch_identity_classifies(
        self, issue_number: int
    ) -> None:
        token = new_scratch_token()
        worktree = scratch_worktree_name("issue-orchestrator", issue_number, token)
        branch = scratch_branch_name(issue_number, token)

        assert (
            classify_timeline_actor(
                {"run_dir": f"/Users/dev/{worktree}/.issue-orchestrator/sessions/r"}
            )
            is TimelineActor.TECH_LEAD_INVESTIGATION
        )
        assert (
            classify_timeline_actor({"branch_name": branch})
            is TimelineActor.TECH_LEAD_INVESTIGATION
        )


class TestFalsePositivesFromCoincidentalPaths:
    """A scratch shape is evidence only for the issue it names (#6969 review F2).

    The pattern can appear anywhere in a path an operator chose. Treating any
    matching ANCESTOR as proof would reclassify every ordinary run beneath it and
    hide real evidence from a health review.
    """

    def test_a_matching_parent_directory_does_not_capture_an_ordinary_run(
        self,
    ) -> None:
        actor = classify_timeline_actor(
            {
                "run_dir": "/srv/customer-tech-lead-7-deadbeefcafe"
                "/issue-orchestrator-6410/.issue-orchestrator/sessions/r"
            },
            issue_number=6410,
        )

        assert actor is not TimelineActor.TECH_LEAD_INVESTIGATION
        assert not actor.is_issue_evidence

    def test_a_real_investigation_under_a_matching_parent_is_still_recognised(
        self,
    ) -> None:
        token = new_scratch_token()
        worktree = scratch_worktree_name("issue-orchestrator", 6410, token)
        actor = classify_timeline_actor(
            {
                "run_dir": f"/srv/customer-tech-lead-7-deadbeefcafe/{worktree}"
                "/.issue-orchestrator/sessions/r"
            },
            issue_number=6410,
        )

        assert actor is TimelineActor.TECH_LEAD_INVESTIGATION

    def test_a_scratch_shape_naming_another_issue_is_ambiguous_not_evidence(
        self,
    ) -> None:
        token = new_scratch_token()
        other = scratch_worktree_name("issue-orchestrator", 99, token)
        actor = classify_timeline_actor(
            {"run_dir": f"/Users/dev/{other}/.issue-orchestrator/sessions/r"},
            issue_number=6410,
        )

        assert actor is TimelineActor.UNKNOWN

    def test_a_branch_naming_another_issue_is_ambiguous_not_evidence(self) -> None:
        actor = classify_timeline_actor(
            {"branch_name": scratch_branch_name(99, new_scratch_token())},
            issue_number=6410,
        )

        assert actor is TimelineActor.UNKNOWN


class TestEveryDurableSignalIsConsulted:
    """A run directory that looks ordinary does not settle the question.

    A validation retry relaunches an investigation on its investigation BRANCH.
    Reading only the first present signal answered "the issue's own work" and put
    the approval and the push back on the implementation (#6969 review F1).
    """

    def test_an_investigation_branch_outweighs_an_ordinary_run_dir(self) -> None:
        actor = classify_timeline_actor(
            {
                "run_dir": IMPLEMENTATION_RUN_DIR,
                "branch_name": "tech-lead-investigation-6410-df24fde45b3b",
            },
            issue_number=6410,
        )

        assert actor is TimelineActor.TECH_LEAD_INVESTIGATION

    def test_an_ordinary_branch_and_run_dir_is_the_issues_own_work(self) -> None:
        actor = classify_timeline_actor(
            {"run_dir": IMPLEMENTATION_RUN_DIR, "branch_name": "6410-fix-the-thing"},
            issue_number=6410,
        )

        assert actor is TimelineActor.ISSUE_SESSION


class TestReaderNeverRaises:
    """One unreadable row must not take an aggregate down (#6969 review F3).

    The board snapshot is a REQUIRED input to every tech-lead launch, so a value
    from a newer writer aborting snapshot construction would strand the queued
    tech-lead work as needs-human — a denial of service on the subsystem the
    discriminator exists to protect.
    """

    def test_a_future_actor_value_reads_as_unattributable(self) -> None:
        actor = read_timeline_actor(
            {TIMELINE_ACTOR_FIELD: "reviewer-session"}, issue_number=6410
        )

        assert actor is TimelineActor.UNKNOWN
        assert not actor.is_issue_evidence

    def test_a_malformed_actor_value_reads_as_unattributable(self) -> None:
        assert (
            read_timeline_actor({TIMELINE_ACTOR_FIELD: 7}, issue_number=6410)
            is TimelineActor.UNKNOWN
        )

    def test_a_producer_still_refuses_to_persist_an_unknown_value(self) -> None:
        # The strictness moves to the write side rather than disappearing.
        with pytest.raises(ValueError):
            classify_timeline_actor(
                {TIMELINE_ACTOR_FIELD: "reviewer-session"}, issue_number=6410
            )
